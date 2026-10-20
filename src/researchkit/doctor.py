"""Preflight checks for a preset's model slots and harness CLIs.

``researchkit doctor`` validates the active (or ``--preset``) configuration
BEFORE any subscription/API spend: are the CLI harnesses installed and logged
in, do their pinned model ids still exist (harness ids drift — ``grok-build``
died, Antigravity switched to display names), and which API providers will be
skipped for missing keys. Fail in seconds, not twenty minutes into a run.

Checks are static + cheap CLI list commands only — no completions are run and
no tokens are spent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass

from researchkit.safe_io import claude_subscription_env, run_subprocess
from researchkit.system_config import EffectiveModels, SystemConfigManager

_CLI_TIMEOUT = 20.0

# Provider slot -> env vars that activate it (any one suffices). The claude
# slot is CLI-backed by design and has no key.
_SLOT_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "grok": ("XAI_API_KEY",),
    "perplexity": ("PERPLEXITY_API_KEY",),
    "tavily": ("TAVILY_API_KEY",),
    "github": ("GITHUB_TOKEN", "GH_TOKEN"),
    "glm": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY"),
    "kimi": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
}

# Plain model-id prefix -> env vars, for the summarizer/improver/council
# slots whose backend is inferred from the id.
_MODEL_ENV_VARS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("gemini", "models/gemini"), ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    (("grok",), ("XAI_API_KEY",)),
    (("sonar",), ("PERPLEXITY_API_KEY",)),
    (("glm",), ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY")),
    (("kimi", "moonshot"), ("MOONSHOT_API_KEY", "KIMI_API_KEY")),
)
_DEFAULT_MODEL_ENV = ("OPENAI_API_KEY",)


@dataclass
class CheckResult:
    """Outcome of one slot/member preflight check."""

    slot: str
    spec: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""


class _CliProbe:
    """Cached, cheap per-binary probes (one subprocess per CLI per run)."""

    def __init__(self) -> None:
        self._model_lists: dict[tuple[str, tuple[str, ...]], tuple[bool, str]] = {}

    @staticmethod
    def binary_missing(binary: str) -> bool:
        return shutil.which(binary) is None

    def model_list(self, binary: str, args: list[str]) -> tuple[bool, str]:
        """Run a CLI's list-models command once; returns (ok, output/error)."""
        key = (binary, tuple(args))
        if key not in self._model_lists:
            try:
                proc = run_subprocess([binary, *args], timeout=_CLI_TIMEOUT)
            except Exception as e:  # timeout, spawn failure
                self._model_lists[key] = (False, str(e)[:200])
            else:
                ok = proc.returncode == 0
                text = proc.stdout if ok else (proc.stderr or proc.stdout)
                self._model_lists[key] = (ok, text.strip()[:2000])
        return self._model_lists[key]

    def check_listable(
        self, binary: str, list_args: list[str], model: str | None
    ) -> tuple[str, str]:
        """Status for a CLI whose models can be listed (grok, agy)."""
        if self.binary_missing(binary):
            return "fail", f"`{binary}` not found on PATH"
        ok, output = self.model_list(binary, list_args)
        if not ok:
            return "fail", f"`{binary} {' '.join(list_args)}` failed: {output[:160]}"
        if model and not _model_listed(model, output):
            # Whole-token match: substring would false-pass a dead `grok-4`
            # pin against a listed `grok-4-fast` — the deprecation pattern
            # this check exists to catch.
            return (
                "fail",
                f"model {model!r} not in `{binary} {' '.join(list_args)}` output",
            )
        return "ok", "logged in" + (f"; model {model!r} listed" if model else "")

    def check_binary_only(self, binary: str, note: str) -> tuple[str, str]:
        if self.binary_missing(binary):
            return "fail", f"`{binary}` not found on PATH"
        return "ok", note

    def check_claude_auth(self) -> tuple[str, str]:
        """Status for the Claude Code CLI: binary present AND logged in.

        `claude auth status` is local and free, and a logged-out CLI fails
        every writer, the council boss and the super-summary of every run.
        """
        if self.binary_missing("claude"):
            return "fail", "`claude` not found on PATH"
        logged_in, detail = claude_auth_status()
        if logged_in is False:
            return "fail", detail
        return "ok", detail


def claude_auth_status(binary: str = "claude") -> tuple[bool | None, str]:
    """(logged_in, detail) from `claude auth status`; None when unverifiable."""
    try:
        # Same env the real calls get, so this reports the auth they will use.
        proc = run_subprocess(
            [binary, "auth", "status"],
            timeout=_CLI_TIMEOUT,
            env=claude_subscription_env(),
        )
    except Exception as e:  # timeout, spawn failure
        return None, f"installed (auth not verifiable: {str(e)[:120]})"
    try:
        info = json.loads(proc.stdout or "")
    except ValueError:
        return (
            None,
            "installed (auth not verifiable: `claude auth status` gave no JSON)",
        )
    if not isinstance(info, dict):
        return None, "installed (auth not verifiable: unexpected `claude auth status`)"
    if not info.get("loggedIn"):
        return (
            False,
            "not logged in — run `claude auth login` (or /login in Claude Code)",
        )
    # The env scrub can't reach settings-level routes (apiKeyHelper, a cloud
    # provider): those still report api_key / a non-firstParty provider here.
    provider = info.get("apiProvider", "firstParty")
    if info.get("authMethod") == "api_key" or provider != "firstParty":
        source = info.get("apiKeySource") or provider
        return (
            False,
            f"Claude Code would bill an API ({source}), not the subscription — "
            "remove it from Claude Code settings and run `claude auth login`",
        )
    via = ", ".join(
        str(info[k]) for k in ("authMethod", "subscriptionType") if info.get(k)
    )
    return True, "logged in" + (f" ({via})" if via else "")


def _model_listed(model: str, output: str) -> bool:
    """True when ``model`` appears as a whole token/name in the CLI output.

    Substring matching false-passes a dead ``grok-4`` pin against a listed
    ``grok-4.5``. Ids match as whole tokens — including the id column of
    ``agy models``' ``<id>\t<display name>`` lines. A spec containing spaces
    must match a whole line, which is what makes a legacy agy display-name
    pin ("Gemini 3.5 Flash (High)") fail loudly now that agy lists ids.
    """
    if " " in model:
        return any(line.strip() == model for line in output.splitlines())
    return re.search(rf"(?<![\w.\-]){re.escape(model)}(?![\w.\-])", output) is not None


def _env_present(candidates: tuple[str, ...]) -> str | None:
    for var in candidates:
        if os.getenv(var):
            return var
    return None


def _check_api_key(
    slot: str, spec: str, candidates: tuple[str, ...], *, skippable: bool
) -> CheckResult:
    found = _env_present(candidates)
    if found:
        return CheckResult(slot, spec, "ok", f"{found} set")
    names = " / ".join(candidates)
    if skippable:
        return CheckResult(
            slot, spec, "warn", f"no key ({names}) — provider will be skipped"
        )
    return CheckResult(slot, spec, "fail", f"no key ({names})")


def _model_env_vars(model: str) -> tuple[str, ...]:
    m = model.lower()
    for prefixes, env_vars in _MODEL_ENV_VARS:
        if m.startswith(prefixes):
            return env_vars
    return _DEFAULT_MODEL_ENV


def _check_spec(slot: str, spec: str, probe: _CliProbe) -> CheckResult:
    """Check one model spec (CLI-backed or plain API id)."""
    from researchkit.council import split_effort_spec
    from researchkit.providers import (
        is_antigravity_model,
        is_codex_model,
        is_grokcli_model,
        is_kimicli_model,
    )
    from researchkit.providers.antigravity_provider import antigravity_underlying_model
    from researchkit.providers.grokcli_provider import grokcli_underlying_model

    base, _ = split_effort_spec(spec)
    if is_grokcli_model(base):
        status, detail = probe.check_listable(
            "grok", ["models"], grokcli_underlying_model(base)
        )
        return CheckResult(slot, spec, status, detail)
    if is_antigravity_model(base):
        status, detail = probe.check_listable(
            "agy", ["models"], antigravity_underlying_model(base)
        )
        return CheckResult(slot, spec, status, detail)
    if is_codex_model(base):
        status, detail = probe.check_binary_only(
            "codex", "installed (auth/model not verifiable pre-spend)"
        )
        return CheckResult(slot, spec, status, detail)
    if is_kimicli_model(base):
        status, detail = probe.check_binary_only(
            "kimi", "installed (auth not verifiable; aliases live in config.toml)"
        )
        return CheckResult(slot, spec, status, detail)
    if base.lower().startswith(("claude", "deep")):
        status, detail = probe.check_claude_auth()
        return CheckResult(slot, spec, status, detail)
    return _check_api_key(slot, spec, _model_env_vars(base), skippable=False)


def run_doctor(preset_name: str | None = None) -> list[CheckResult]:
    """Run every preflight check for the preset; no tokens are spent."""
    em: EffectiveModels = SystemConfigManager().resolve_effective_models(preset_name)
    probe = _CliProbe()
    results: list[CheckResult] = []

    # Provider slots. CLI-backed specs get CLI checks; plain ids get key
    # checks (missing key = warn: providers are skipped gracefully by design).
    from researchkit.council import is_cli_backed_spec

    def _provider_slot(slot: str, spec: str) -> CheckResult:
        if not is_cli_backed_spec(spec):
            return _check_api_key(slot, spec, _SLOT_ENV_VARS[slot], skippable=True)
        result = _check_spec(slot, spec, probe)
        # Policy parity with missing keys: a provider CLI that simply isn't
        # installed means the provider is skipped, not that the run is
        # broken. A DEAD MODEL ID or failed login stays a hard fail — that
        # drift is what doctor exists to catch.
        if result.status == "fail" and "not found on PATH" in result.detail:
            return CheckResult(
                slot, spec, "warn", f"{result.detail} — provider will fail if selected"
            )
        return result

    for slot in ("openai", "gemini", "grok", "perplexity", "tavily", "github", "glm"):
        results.append(_provider_slot(slot, getattr(em, slot)))
    # The claude slot is CLI-backed in every spelling (claude:/bare/deep:) —
    # `deep:` doesn't match is_cli_backed_spec, so route it directly.
    results.append(_check_spec("claude", em.claude, probe))
    results.append(_provider_slot("kimi", em.kimi))
    # The overview/digest/article/skill writers run on the Claude Code CLI in
    # EVERY preset, so its login matters even when the claude slot is an API id.
    results.append(CheckResult("writers", "claude-code", *probe.check_claude_auth()))

    # Pipeline model slots: these aren't skipped gracefully — a broken
    # summarizer/improver degrades every run.
    for slot in ("summarizer", "site_summarizer", "improver"):
        results.append(_check_spec(slot, getattr(em, slot), probe))

    # Council members + boss (advise/council/explore).
    for i, member in enumerate(em.council_members, 1):
        results.append(_check_spec(f"council[{i}]", member, probe))
    results.append(_check_spec("boss", em.council_boss, probe))
    return _apply_harness_only(em, results)


def _apply_harness_only(
    em: EffectiveModels, results: list[CheckResult]
) -> list[CheckResult]:
    """On a harness_only preset, flag every LLM step that would bill an API.

    Pipeline steps (summarizers, improver, council) run on every run: fail.
    A research slot only runs when selected, and the run refuses it: warn.
    """
    if not em.harness_only:
        return results
    from researchkit.plugins import get_registry

    pipeline = set(em.api_billed_llm_slots(()))
    research = get_registry().llm_provider_names() | {"github"}
    flagged: dict[str, CheckResult] = {}
    for entry in em.api_billed_llm_slots(sorted(research)):
        slot, _, spec = entry.partition("=")
        if entry in pipeline:
            detail = "harness_only preset: this LLM step would bill an API"
            flagged[slot] = CheckResult(slot, spec, "fail", detail)
        else:
            detail = "no harness route — a harness_only run refuses it if selected"
            flagged[slot] = CheckResult(slot, spec, "warn", detail)
    return [flagged.pop(r.slot, r) for r in results] + list(flagged.values())


def format_report(results: list[CheckResult], preset_name: str) -> str:
    """Human-readable doctor report."""
    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    width = max(len(r.slot) for r in results)
    lines = [f"researchkit doctor — preset '{preset_name}'", ""]
    for r in results:
        lines.append(f"  {icon[r.status]} {r.slot:<{width}}  {r.spec}  — {r.detail}")
    fails = sum(1 for r in results if r.status == "fail")
    warns = sum(1 for r in results if r.status == "warn")
    lines.append("")
    lines.append(
        f"{len(results)} checks: {len(results) - fails - warns} ok, "
        f"{warns} warnings, {fails} failures"
    )
    return "\n".join(lines)
