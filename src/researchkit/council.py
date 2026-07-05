"""LLM council for topic improvement and keyword generation.

A council of several models each analyze a research topic independently through
distinct cognitive lenses (Stage 1), then a designated *boss* model synthesizes a
single decisive result (Stage 2): a refined topic, search keywords, and a call on
whether the topic is worth decomposing into parallel sub-projects (boost mode).

Members are model specs and may be CLI-backed:
  - ``codex`` / ``codex:<model>``  -> Codex CLI (``codex exec``), no web search
  - ``agy`` / ``agy:<model>``      -> Antigravity CLI (``agy --print``)
  - ``claude*``                    -> Claude Code CLI (``claude -p``), no web tools
  - plain API ids (gpt-*, gemini-*, grok*, sonar*, glm-*) -> the provider API

The council is a drop-in replacement for :class:`PromptImprover`: it exposes
``improve_topic`` and ``generate_keywords``, plus :meth:`deliberate` which returns
the full :class:`CouncilResult` (including the decomposition decision).
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from researchkit.prompts import (
    COUNCIL_LENSES,
    get_council_boss_system_prompt,
    get_council_boss_user_prompt,
    get_council_member_system_prompt,
    get_council_member_user_prompt,
)
from researchkit.safe_io import run_subprocess

if TYPE_CHECKING:
    from researchkit.system_config import EffectiveModels, SystemConfigManager

logger = logging.getLogger(__name__)

_JSON_INSTRUCTION = "\n\nRespond ONLY with the JSON object, no preamble or fences."
_CLAUDE_CLI_TIMEOUT = 300.0
_DEFAULT_KEYWORD_COUNT = 10


@dataclass
class CouncilProposal:
    """One council member's independent proposal for a topic."""

    member: str
    lens: str
    improved_topic: str = ""
    keywords: list[str] = field(default_factory=list)
    decompose: bool = False
    subqueries: list[str] = field(default_factory=list)
    rationale: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.improved_topic)


@dataclass
class CouncilResult:
    """The boss-synthesized final result of a council deliberation."""

    improved_topic: str
    keywords: list[str]
    decompose: bool = False
    subqueries: list[str] = field(default_factory=list)
    rationale: str = ""
    convergence: str = ""
    proposals: list[CouncilProposal] = field(default_factory=list)
    boss_synthesized: bool = True  # False if the boss failed and we merged manually

    def to_dict(self) -> dict[str, Any]:
        return {
            "improved_topic": self.improved_topic,
            "keywords": self.keywords,
            "decompose": self.decompose,
            "subqueries": self.subqueries,
            "rationale": self.rationale,
            "convergence": self.convergence,
            "boss_synthesized": self.boss_synthesized,
            "proposals": [
                {
                    "member": p.member,
                    "lens": p.lens,
                    "improved_topic": p.improved_topic,
                    "keywords": p.keywords,
                    "decompose": p.decompose,
                    "subqueries": p.subqueries,
                    "rationale": p.rationale,
                    "error": p.error,
                }
                for p in self.proposals
            ],
        }


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a model response (tolerates fences/prose)."""
    if not text:
        return None
    candidate = text.strip()
    # Strip common ```json fences.
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate).strip()
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Scan for the first COMPLETE JSON object. raw_decode stops at the end of the
    # first object, so this tolerates prose/thinking blocks or trailing objects
    # around the JSON (a greedy {.*} regex would wrongly span multiple objects).
    decoder = json.JSONDecoder()
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    logger.warning("council: failed to parse JSON from response: %s", text[:300])
    return None


def _coerce_decompose(raw: Any) -> bool:
    """Coerce a model-supplied ``decompose`` value to bool, safely.

    ``bool("false")`` is True in Python, so a model that returns the *string*
    ``"false"`` would wrongly trigger a (paid) boost fan-out. Accept only real
    booleans and the string ``"true"`` (case-insensitive). (Review L8.)
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    return False


def _clean_keywords(raw: Any, count: int) -> list[str]:
    """Normalize a keywords field into a deduped, capped list of >=2-word queries."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for k in raw:
        s = str(k).strip()
        if not s or len(s.split()) < 2:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= count:
            break
    return out


def _clean_subqueries(raw: Any, limit: int) -> list[str]:
    """Normalize a subqueries field into a deduped, capped list of sub-topics."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for q in raw:
        s = str(q).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


class LLMCouncil:
    """A council of models that improves topics and generates keywords."""

    def __init__(
        self,
        members: list[str],
        boss: str,
        max_subprojects: int = 5,
        claude_budget: float = 3.0,
    ) -> None:
        self.members = [m for m in members if m and m.strip()]
        self.boss = boss
        self.max_subprojects = max(2, max_subprojects)
        self.claude_budget = claude_budget

    @classmethod
    def from_effective_models(cls, effective_models: EffectiveModels) -> LLMCouncil:
        """Create a council from a resolved :class:`EffectiveModels`."""
        return cls(
            members=list(effective_models.council_members),
            boss=effective_models.council_boss,
            max_subprojects=effective_models.boost_max_subprojects,
            claude_budget=min(effective_models.claude_max_budget, 3.0),
        )

    @classmethod
    def from_system_config(
        cls, config_manager: SystemConfigManager | None = None
    ) -> LLMCouncil:
        """Create a council from the active models.yaml preset."""
        from researchkit.system_config import SystemConfigManager

        mgr = config_manager or SystemConfigManager()
        return cls.from_effective_models(mgr.resolve_effective_models())

    # -- backend routing ---------------------------------------------------

    def _complete(
        self, model_spec: str, system_prompt: str, user_prompt: str, label: str
    ) -> str:
        """Run a single non-search completion through the right backend."""
        from researchkit.providers.antigravity_provider import (
            AntigravityProvider,
            antigravity_underlying_model,
            is_antigravity_model,
        )
        from researchkit.providers.codex_provider import (
            CodexProvider,
            codex_underlying_model,
            is_codex_model,
        )

        combined = f"{system_prompt}\n\n{user_prompt}{_JSON_INSTRUCTION}"

        if is_codex_model(model_spec):
            codex = CodexProvider(model=codex_underlying_model(model_spec))
            text, _ = codex._exec(combined, web_search=False, label=label)
            return text
        if is_antigravity_model(model_spec):
            agy = AntigravityProvider(model=antigravity_underlying_model(model_spec))
            return agy._run_cli(combined, label=label)
        if model_spec.lower().startswith("claude"):
            return self._run_claude_cli(
                system_prompt, f"{user_prompt}{_JSON_INSTRUCTION}", model_spec
            )
        # Plain API id -> route through PromptImprover's provider backends.
        return self._run_api(model_spec, system_prompt, user_prompt)

    def _run_claude_cli(self, system_prompt: str, user_prompt: str, model: str) -> str:
        """Run a plain (no web tools) Claude Code completion and return its text."""
        cmd = [
            "claude",
            "-p",
            "--model",
            model,
            "--system-prompt",
            system_prompt,
            "--no-session-persistence",
            "--strict-mcp-config",  # built-in tools only (review M8)
            "--disallowed-tools",
            "WebSearch,WebFetch,Write,Edit,Bash,Read,Glob,Grep,NotebookEdit,Agent",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "json",
            "--max-budget-usd",
            str(self.claude_budget),
        ]
        env = {**os.environ}
        env.pop("ANTHROPIC_API_KEY", None)
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        # Own process group + kill-on-timeout (C2), UTF-8 decode (L26).
        proc = run_subprocess(
            cmd,
            input=user_prompt,
            timeout=_CLAUDE_CLI_TIMEOUT,
            env=env,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {detail[-400:]}")
        try:
            output = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Not the expected JSON wrapper (e.g. truncated/crashed CLI). Log the
            # full streams for debugging, then return raw stdout for best-effort parse.
            logger.warning(
                "council: claude CLI did not return JSON wrapper. stdout=%s stderr=%s",
                proc.stdout[:1000],
                proc.stderr[-500:],
            )
            return proc.stdout.strip()
        if isinstance(output, dict):
            if output.get("is_error"):
                raise RuntimeError(
                    f"claude CLI error: {output.get('result', 'unknown')}"
                )
            result = output.get("result", proc.stdout.strip())
            return result if isinstance(result, str) else str(result)
        return proc.stdout.strip()

    @staticmethod
    def _guess_api_provider(model: str) -> str:
        """Map a plain model id to a PromptImprover provider name."""
        m = model.lower()
        if m.startswith(("gemini", "models/gemini")):
            return "gemini"
        if m.startswith("grok"):
            return "grok"
        if m.startswith("sonar"):
            return "perplexity"
        if m.startswith("glm"):
            return "glm"
        return "openai"

    def _run_api(self, model: str, system_prompt: str, user_prompt: str) -> str:
        """Run a completion through PromptImprover's API backends for plain ids."""
        from researchkit.prompt_improver import PromptImprover

        provider = self._guess_api_provider(model)
        improver = PromptImprover(provider=provider, model=model)
        return improver._call_provider(system_prompt, user_prompt)

    # -- deliberation ------------------------------------------------------

    def _gather_proposals(self, topic: str, count: int) -> list[CouncilProposal]:
        """Stage 1: every member proposes independently, in parallel."""
        user_prompt = get_council_member_user_prompt(topic, count, self.max_subprojects)

        def run_member(idx: int, member: str) -> CouncilProposal:
            lens_name, lens_instruction = COUNCIL_LENSES[idx % len(COUNCIL_LENSES)]
            proposal = CouncilProposal(member=member, lens=lens_name)
            try:
                system_prompt = get_council_member_system_prompt(
                    lens_name, lens_instruction
                )
                text = self._complete(
                    member, system_prompt, user_prompt, label=f"council.member:{member}"
                )
                data = _extract_json(text)
                if not data:
                    proposal.error = f"unparseable response: {text[:500]!r}"
                    return proposal
                proposal.improved_topic = str(
                    data.get("improved_topic") or topic
                ).strip()
                proposal.keywords = _clean_keywords(data.get("keywords"), count)
                proposal.decompose = _coerce_decompose(data.get("decompose"))
                proposal.subqueries = _clean_subqueries(
                    data.get("subqueries"), self.max_subprojects
                )
                proposal.rationale = str(data.get("rationale") or "").strip()
            except Exception as e:
                proposal.error = str(e)
                logger.warning("council member %s failed: %s", member, e)
            return proposal

        with ThreadPoolExecutor(max_workers=max(1, len(self.members))) as pool:
            futures = [
                pool.submit(run_member, i, m) for i, m in enumerate(self.members)
            ]
            return [f.result() for f in futures]

    def _format_proposals(self, proposals: list[CouncilProposal]) -> str:
        """Render valid proposals as an anonymized block for the boss."""
        blocks: list[str] = []
        for i, p in enumerate(proposals):
            label = chr(ord("A") + i)
            sub = "; ".join(p.subqueries) if p.subqueries else "(none)"
            blocks.append(
                f"### Proposal {label} (lens: {p.lens})\n"
                f"- improved_topic: {p.improved_topic}\n"
                f"- keywords: {', '.join(p.keywords) if p.keywords else '(none)'}\n"
                f"- decompose: {p.decompose}\n"
                f"- subqueries: {sub}\n"
                f"- rationale: {p.rationale or '(none)'}"
            )
        return "\n\n".join(blocks)

    def _boss_synthesize(
        self, topic: str, valid: list[CouncilProposal], count: int
    ) -> CouncilResult | None:
        """Stage 2: the boss synthesizes a decisive result from valid proposals."""
        proposals_block = self._format_proposals(valid)
        system_prompt = get_council_boss_system_prompt()
        user_prompt = get_council_boss_user_prompt(
            topic, proposals_block, count, self.max_subprojects
        )
        try:
            text = self._complete(
                self.boss, system_prompt, user_prompt, label=f"council.boss:{self.boss}"
            )
        except Exception as e:
            logger.warning("council boss %s failed: %s", self.boss, e)
            return None
        data = _extract_json(text)
        if not data:
            return None
        improved = str(data.get("improved_topic") or topic).strip()
        keywords = _clean_keywords(data.get("keywords"), count)
        decompose = _coerce_decompose(data.get("decompose"))
        subqueries = _clean_subqueries(data.get("subqueries"), self.max_subprojects)
        # Guard: decomposition needs at least 2 distinct sub-topics to be meaningful.
        if decompose and len(subqueries) < 2:
            decompose = False
            subqueries = []
        return CouncilResult(
            improved_topic=improved or topic,
            keywords=keywords,
            decompose=decompose,
            subqueries=subqueries if decompose else [],
            rationale=str(data.get("rationale") or "").strip(),
            convergence=str(data.get("convergence") or "").strip(),
            boss_synthesized=True,
        )

    def _merge_without_boss(
        self, topic: str, valid: list[CouncilProposal], count: int
    ) -> CouncilResult:
        """Fallback synthesis when the boss call fails: merge proposals heuristically."""
        # Improved topic: the longest non-trivial proposal (most refined), else raw.
        best = max(valid, key=lambda p: len(p.improved_topic), default=None)
        improved = best.improved_topic if best else topic
        # Keywords: union across members, deduped, capped.
        merged: list[str] = []
        seen: set[str] = set()
        for p in valid:
            for k in p.keywords:
                key = k.lower()
                if key not in seen:
                    seen.add(key)
                    merged.append(k)
        merged = merged[:count]
        # Decompose: majority vote; subqueries from the first decomposing proposal.
        votes = sum(1 for p in valid if p.decompose)
        decompose = votes * 2 > len(valid)
        subqueries: list[str] = []
        if decompose:
            for p in valid:
                if p.decompose and len(p.subqueries) >= 2:
                    subqueries = p.subqueries[: self.max_subprojects]
                    break
            if len(subqueries) < 2:
                decompose = False
        return CouncilResult(
            improved_topic=improved or topic,
            keywords=merged,
            decompose=decompose,
            subqueries=subqueries,
            rationale="Boss synthesis unavailable; merged member proposals.",
            convergence="unknown",
            boss_synthesized=False,
        )

    def _single_model_fallback(self, topic: str, count: int) -> CouncilResult:
        """Last resort when every member failed: use the single-model improver."""
        from researchkit.prompt_improver import PromptImprover

        logger.warning("council: all members failed; falling back to single improver")
        try:
            improver = PromptImprover.from_system_config()
            improved = improver.improve_topic(topic)
            keywords = improver.generate_keywords(topic, count=count)
        except Exception as e:
            logger.error("council single-model fallback failed: %s", e)
            improved, keywords = topic, []
        return CouncilResult(
            improved_topic=improved or topic,
            keywords=keywords,
            decompose=False,
            subqueries=[],
            rationale="Council unavailable; single-model fallback.",
            convergence="n/a",
            boss_synthesized=False,
        )

    def deliberate(
        self, topic: str, count: int = _DEFAULT_KEYWORD_COUNT
    ) -> CouncilResult:
        """Run the full council: members propose, then the boss synthesizes."""
        if not topic.strip():
            return CouncilResult(improved_topic=topic, keywords=[])
        if not self.members:
            return self._single_model_fallback(topic, count)

        proposals = self._gather_proposals(topic, count)
        valid = [p for p in proposals if p.ok]
        logger.info(
            "council: %d/%d members responded (members=%s, boss=%s)",
            len(valid),
            len(proposals),
            ", ".join(self.members),
            self.boss,
        )
        if not valid:
            result = self._single_model_fallback(topic, count)
            result.proposals = proposals
            return result

        synthesized = self._boss_synthesize(topic, valid, count)
        final = synthesized or self._merge_without_boss(topic, valid, count)
        final.proposals = proposals
        return final

    # -- PromptImprover-compatible interface -------------------------------

    def improve_topic(self, topic: str) -> str:
        """Return the council's refined topic (PromptImprover-compatible)."""
        return self.deliberate(topic).improved_topic

    def generate_keywords(
        self, topic: str, count: int = _DEFAULT_KEYWORD_COUNT
    ) -> list[str]:
        """Return the council's synthesized keywords (PromptImprover-compatible)."""
        return self.deliberate(topic, count=count).keywords
