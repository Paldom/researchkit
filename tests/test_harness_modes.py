"""Tests for the subscription-only harness modes (advise / council / explore)."""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

import researchkit.cli as cli
import researchkit.council as council_mod
from researchkit.council import (
    ConsultAnswer,
    LLMCouncil,
    complete_via_spec,
    is_cli_backed_spec,
    split_effort_spec,
)


class TestEffortSpec:
    def test_split(self) -> None:
        assert split_effort_spec("codex:gpt-5.6-sol@xhigh") == (
            "codex:gpt-5.6-sol",
            "xhigh",
        )
        assert split_effort_spec("claude-opus-4-8@XHIGH") == (
            "claude-opus-4-8",
            "xhigh",
        )
        assert split_effort_spec("grokcli:grok-build") == ("grokcli:grok-build", None)
        # non-alpha suffix is part of the model id, not an effort
        assert split_effort_spec("weird@3.5") == ("weird@3.5", None)

    def test_is_cli_backed(self) -> None:
        for spec in (
            "claude:claude-opus-4-8@xhigh",
            "claude-opus-4-8@xhigh",  # legacy spelling still routes to the CLI
            "codex:gpt-5.6-sol@xhigh",
            "agy:Gemini 3.5 Flash (High)",
            "grokcli:grok-4.5",
            "kimicli:kimi-code/k3",
            "kimicli",
            "codex",
            "claude",
        ):
            assert is_cli_backed_spec(spec), spec
        for spec in (
            "gpt-5.5",
            "gemini-3.5-flash",
            "grok-4.3",
            "sonar",
            "kimi-k2.6",  # plain Kimi API id, not the CLI spec
            None,
            "",
        ):
            assert not is_cli_backed_spec(spec), spec


class TestRouterEffort:
    def test_codex_and_grokcli_receive_effort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str | None]] = []

        def fake_exec(self, prompt: str, *, web_search: bool, label: str):
            seen.append((type(self).__name__, self.reasoning_effort))
            return "ok", []

        from researchkit.providers.codex_provider import CodexProvider
        from researchkit.providers.grokcli_provider import GrokCliProvider

        monkeypatch.setattr(CodexProvider, "_exec", fake_exec)
        monkeypatch.setattr(GrokCliProvider, "_exec", fake_exec)
        assert complete_via_spec("codex:gpt-5.6-sol@xhigh", "s", "u", label="t") == "ok"
        assert complete_via_spec("grokcli:grok-build", "s", "u", label="t") == "ok"
        assert seen == [("CodexProvider", "xhigh"), ("GrokCliProvider", None)]

    def test_claude_canonical_spec_unwraps_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any):
            captured.append(cmd)
            import subprocess

            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"result": "hi"}), stderr=""
            )

        monkeypatch.setattr(council_mod, "run_subprocess", fake_run)
        # canonical harness-pattern spec: model unwrapped after the prefix
        assert (
            complete_via_spec("claude:claude-opus-4-8@xhigh", "s", "u", label="t")
            == "hi"
        )
        cmd = captured[0]
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
        assert cmd[cmd.index("--effort") + 1] == "xhigh"
        # bare `claude` -> CLI default model (no --model flag at all)
        assert complete_via_spec("claude", "s", "u", label="t") == "hi"
        assert "--model" not in captured[1]

    def test_claude_receives_effort_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any):
            captured.append(cmd)
            import subprocess

            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"result": "hi"}), stderr=""
            )

        monkeypatch.setattr(council_mod, "run_subprocess", fake_run)
        out = complete_via_spec(
            "claude-opus-4-8@xhigh", "sys", "user", label="t", claude_budget=9.0
        )
        assert out == "hi"
        cmd = captured[0]
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"  # effort stripped
        assert cmd[cmd.index("--effort") + 1] == "xhigh"
        assert cmd[cmd.index("--max-budget-usd") + 1] == "9.0"


def _fake_member_json(answer: str, confidence: str = "high") -> str:
    return json.dumps(
        {"answer": answer, "confidence": confidence, "rationale": "because"}
    )


class TestConsult:
    def _council(self) -> LLMCouncil:
        return LLMCouncil(
            members=["m1", "m2", "m3"], boss="boss-model", claude_budget=3.0
        )

    def test_boss_synthesis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_complete(self, spec, system, user, label):
            if spec == "boss-model":
                return json.dumps(
                    {
                        "answer": "synthesized",
                        "confidence": "high",
                        "convergence": "medium",
                        "dissent": "m2 disagrees",
                    }
                )
            return _fake_member_json(f"answer from {spec}")

        monkeypatch.setattr(LLMCouncil, "_complete", fake_complete)
        result = self._council().consult("what should we do?")
        assert result.answer == "synthesized"
        assert result.boss_synthesized
        assert result.dissent == "m2 disagrees"
        assert len(result.answers) == 3
        assert {a.lens for a in result.answers} == {
            "Direct & Practical",
            "Skeptic & Risks",
            "Context & Tradeoffs",
        }

    def test_boss_failure_falls_back_to_first_valid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_complete(self, spec, system, user, label):
            if spec == "boss-model":
                raise RuntimeError("boss down")
            if spec == "m1":
                raise RuntimeError("m1 down")
            return _fake_member_json(f"answer from {spec}")

        monkeypatch.setattr(LLMCouncil, "_complete", fake_complete)
        result = self._council().consult("q")
        # deterministic: first VALID member in configured order, never longest
        assert result.answer == "answer from m2"
        assert not result.boss_synthesized
        assert result.answers[0].error is not None

    def test_semantically_empty_boss_verdict_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_complete(self, spec, system, user, label):
            if spec == "boss-model":
                return json.dumps({"answer": "   ", "confidence": "high"})
            return _fake_member_json(f"answer from {spec}")

        monkeypatch.setattr(LLMCouncil, "_complete", fake_complete)
        result = self._council().consult("q")
        assert not result.boss_synthesized
        assert result.answer == "answer from m1"

    def test_all_members_failed_raises_with_details(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_complete(self, spec, system, user, label):
            raise RuntimeError(f"{spec} unavailable")

        monkeypatch.setattr(LLMCouncil, "_complete", fake_complete)
        with pytest.raises(RuntimeError, match="m1 unavailable"):
            self._council().consult("q")


class TestAdvise:
    def test_gathers_each_answer_and_isolates_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_complete(spec, system, user, *, label, claude_budget=3.0):
            if spec == "m2":
                raise RuntimeError("cli missing")
            return f"answer from {spec}"

        monkeypatch.setattr(council_mod, "complete_via_spec", fake_complete)
        council = LLMCouncil(members=["m1", "m2"], boss="b")
        answers = council.advise("q")
        assert [a.member for a in answers] == ["m1", "m2"]
        assert answers[0].ok and answers[0].answer == "answer from m1"
        assert not answers[1].ok and "cli missing" in (answers[1].error or "")


class TestSummarizerCliRouting:
    def test_cli_spec_routes_through_router(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import researchkit.summarizer as summarizer_mod
        from researchkit.summarizer import Summarizer

        calls: list[str] = []

        def fake_complete(spec, system, user, *, label, claude_budget=3.0):
            calls.append(spec)
            return "meta summary text"

        monkeypatch.setattr(council_mod, "complete_via_spec", fake_complete)
        s = Summarizer(model="agy:gemini-3.5-flash")
        assert s._is_cli
        assert s._get_client() is None  # no API client, no key required
        out = s._generate(
            None, "prompt", label="t", temperature=0.2, max_output_tokens=100
        )
        assert out == "meta summary text"
        assert calls == ["agy:gemini-3.5-flash"]
        assert summarizer_mod  # imported for parity with other tests

    def test_plain_model_still_uses_api_client(self) -> None:
        from researchkit.summarizer import Summarizer

        s = Summarizer(model="gemini-3.5-flash", api_key="k")
        assert not s._is_cli


class TestExploreCommand:
    def test_forces_harness_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_boosted(args: Any, service: Any, topic: str) -> int:
            captured.update(vars(args))
            captured["topic"] = topic
            return 0

        monkeypatch.setattr(cli, "_cmd_instant_boosted", fake_boosted)
        args = argparse.Namespace(
            topic="agent memory",
            preset="harness",
            days=7,
            materials=False,
            materials_limit=25,
            ingest=None,
            no_raw=False,
            verbose=False,
            log_level="INFO",
        )
        assert cli.cmd_explore(args, service=None) == 0
        assert captured["topic"] == "agent memory"
        assert captured["boost"] is True
        assert captured["providers"] == ["openai", "gemini", "grok", "claude", "kimi"]
        assert captured["no_site_research"] is True
        assert captured["preset_name"] == "harness"


class TestHarnessPreset:
    def test_preset_is_fully_cli_backed(self) -> None:
        from researchkit.system_config import SystemConfigManager

        em = SystemConfigManager().resolve_effective_models("harness")
        # every slot the harness flows actually use routes to a CLI
        for spec in (em.openai, em.gemini, em.grok, em.claude, em.kimi, em.summarizer):
            assert is_cli_backed_spec(spec), spec
        for member in em.council_members:
            assert is_cli_backed_spec(member), member
        assert is_cli_backed_spec(em.council_boss)

    def test_default_members_match_goal_spec(self) -> None:
        from researchkit.system_config import SystemConfigManager

        em = SystemConfigManager().resolve_effective_models("harness")
        assert em.council_members == [
            "claude:claude-opus-4-8@xhigh",
            "codex:gpt-5.6-sol@xhigh",
            "agy:Gemini 3.5 Flash (High)",
            "grokcli:grok-4.5",
            "kimicli:kimi-code/k3",
        ]


class TestClaudeSpecHelpers:
    def test_canonical_spec_matching(self) -> None:
        from researchkit.providers import (
            claude_cli_underlying_model,
            is_claude_cli_spec,
        )

        assert is_claude_cli_spec("claude")
        assert is_claude_cli_spec("claude:opus")
        assert is_claude_cli_spec("CLAUDE:claude-sonnet-4-6")
        assert not is_claude_cli_spec("claude-opus-4-8")  # legacy bare id
        assert not is_claude_cli_spec("codex:gpt-5.5")
        assert not is_claude_cli_spec(None)
        assert claude_cli_underlying_model("claude:claude-opus-4-8") == (
            "claude-opus-4-8"
        )
        assert claude_cli_underlying_model("claude:opus") == "opus"
        assert claude_cli_underlying_model("claude") is None

    def test_factory_unwraps_canonical_spec(self) -> None:
        from researchkit.plugin_api import ProviderContext
        from researchkit.plugins_builtin import _make_claude
        from researchkit.providers import ClaudeProvider

        ctx = ProviderContext(
            model="claude:claude-opus-4-8",
            sources=frozenset({"web"}),
            keywords=(),
            options={"max_budget": 15.0, "reasoning_effort": "xhigh"},
        )
        provider = _make_claude(ctx)
        assert isinstance(provider, ClaudeProvider)
        assert provider.model_name == "claude-opus-4-8"  # prefix unwrapped

    def test_final_summary_unwraps_canonical_slot(self) -> None:
        from researchkit.final_summary import ClaudeFinalSummaryGenerator
        from researchkit.system_config import SystemConfigManager

        em = SystemConfigManager().resolve_effective_models("harness")
        gen = ClaudeFinalSummaryGenerator.from_effective_models(em)
        assert gen.model == "claude-opus-4-8"


class TestSuperSummaryEffortStripping:
    def test_boss_effort_suffix_is_stripped(self) -> None:
        # Red-team B1: 'claude-opus-4-8@xhigh' reached `claude --model`
        # verbatim, killing every boosted harness run's super-summary.
        from researchkit.final_summary import SuperSummaryGenerator
        from researchkit.system_config import SystemConfigManager

        em = SystemConfigManager().resolve_effective_models("harness")
        gen = SuperSummaryGenerator.from_effective_models(em)
        assert gen.model == "claude-opus-4-8"


class TestCliOnlyCouncilFallback:
    def test_all_cli_members_failed_skips_api_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Red-team m1: a subscription-only council must not silently fall
        # back to the API-key improver.
        called: list[str] = []

        def fake_complete(self, spec, system, user, label):
            raise RuntimeError("cli down")

        monkeypatch.setattr(LLMCouncil, "_complete", fake_complete)
        import researchkit.prompt_improver as pi

        monkeypatch.setattr(
            pi.PromptImprover,
            "from_system_config",
            classmethod(lambda cls, *a: called.append("api") or None),
        )
        council = LLMCouncil(
            members=["codex:gpt-5.6-sol@xhigh", "grokcli:grok-build"], boss="codex"
        )
        result = council.deliberate("some topic")
        assert result.improved_topic == "some topic"
        assert "no API fallback" in result.rationale.lower() or not called
        assert called == []


class TestSlotCliRouting:
    """Every model slot accepts harness specs (hybrid/harness presets)."""

    def test_improver_cli_spec_routes_via_router(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchkit.prompt_improver import PromptImprover

        calls: list[str] = []

        def fake(spec, system, user, *, label, claude_budget=3.0):
            calls.append(spec)
            return "improved topic"

        monkeypatch.setattr(council_mod, "complete_via_spec", fake)
        imp = PromptImprover(model="codex:gpt-5.6-sol")
        assert imp.provider == "cli"  # spec overrides the default provider
        assert imp._call_provider("s", "u") == "improved topic"
        assert calls == ["codex:gpt-5.6-sol"]

    def test_improver_from_system_config_cli(self) -> None:
        from researchkit.prompt_improver import PromptImprover
        from researchkit.system_config import SystemConfigManager

        real = SystemConfigManager()

        class HarnessMgr:
            def resolve_effective_models(self, name=None):
                return real.resolve_effective_models("harness")

        imp = PromptImprover.from_system_config(HarnessMgr())  # type: ignore[arg-type]
        assert imp.provider == "cli"
        assert imp.model == "codex:gpt-5.6-sol"

    def test_keyword_synthesizer_cli_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchkit.keyword_synthesizer import KeywordSynthesizer

        def fake(spec, system, user, *, label, claude_budget=3.0):
            return json.dumps({"keywords": ["agent memory", "crypto signals"]})

        monkeypatch.setattr(council_mod, "complete_via_spec", fake)
        ks = KeywordSynthesizer(model="grokcli:grok-build")
        out = ks.synthesize_from_provider_keywords(
            "t", 7, {"gemini": ["agent memory", "other thing"]}
        )
        assert out == ["agent memory", "crypto signals"]

    def test_exa_connector_summarize_cli_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchkit.site_research.connectors.exa import ExaConnector
        from researchkit.site_research.types import SiteItem

        def fake(spec, system, user, *, label, claude_budget=3.0):
            assert spec == "codex:gpt-5.6-sol"
            return "## Key Themes\n- summarized on CLI"

        monkeypatch.setattr(council_mod, "complete_via_spec", fake)
        conn = ExaConnector(api_key="x", gemini_model="codex:gpt-5.6-sol")
        items = [
            SiteItem(url="https://a.test/1", title="A", site="exa", query="q"),
            SiteItem(url="https://a.test/2", title="B", site="exa", query="q"),
        ]
        out = conn.summarize_batch("topic", items)
        assert "summarized on CLI" in out

    def test_github_improver_cli_spec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from researchkit.providers.github_provider import GitHubProvider

        def fake(spec, system, user, *, label, claude_budget=3.0):
            assert spec == "claude:claude-opus-4-8"
            return "- **summary bullet**"

        monkeypatch.setattr(council_mod, "complete_via_spec", fake)
        gh = GitHubProvider(improver_model="claude:claude-opus-4-8")
        assert gh.summarize_result("raw text", "topic") == "- **summary bullet**"

    def test_hybrid_preset_resolves_mixed(self) -> None:
        from researchkit.system_config import SystemConfigManager

        em = SystemConfigManager().resolve_effective_models("hybrid")
        assert is_cli_backed_spec(em.openai) and is_cli_backed_spec(em.grok)
        assert not is_cli_backed_spec(em.gemini)  # API stays API
        assert not is_cli_backed_spec(em.perplexity)
        assert is_cli_backed_spec(em.summarizer)
        assert is_cli_backed_spec(em.improver)
        assert not is_cli_backed_spec(em.site_summarizer)  # volume slot on API
        # harness preset is now fully CLI on every LLM slot
        emh = SystemConfigManager().resolve_effective_models("harness")
        for spec in (emh.summarizer, emh.site_summarizer, emh.improver):
            assert is_cli_backed_spec(spec), spec

    def test_preset_flag_reaches_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class FakeService:
            def create_and_run_project(self, **kwargs: Any):
                captured.update(kwargs)
                raise KeyboardInterrupt  # stop before any output handling

        args = argparse.Namespace(
            topic="t",
            preset="hybrid",
            boost=False,
            verbose=False,
            log_level="INFO",
            keywords=[],
            days=7,
            providers=None,
            sources=None,
            no_raw=False,
            no_site_research=False,
            site_research_sites=None,
            materials=False,
            materials_limit=25,
            ingest=None,
        )
        assert cli.cmd_instant(args, FakeService(), "t") == 130
        assert captured["preset_name"] == "hybrid"


class TestCmdCouncilOutput:
    def test_prints_synthesis_and_members(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from researchkit.council import ConsultResult

        result = ConsultResult(
            answer="Do X.",
            confidence="high",
            convergence="medium",
            dissent="one member prefers Y",
            answers=[
                ConsultAnswer(
                    member="m1",
                    lens="Direct & Practical",
                    answer="Do X.",
                    confidence="high",
                    rationale="works",
                ),
                ConsultAnswer(member="m2", lens="Skeptic & Risks", error="down"),
            ],
        )
        monkeypatch.setattr(LLMCouncil, "consult", lambda self, q: result)
        args = argparse.Namespace(
            question="what?",
            context_file=None,
            harnesses=["m1", "m2"],
            boss="b",
            preset="harness",
            verbose=False,
        )
        assert cli.cmd_council(args) == 0
        out = capsys.readouterr().out
        assert "Council answer" in out and "Do X." in out
        assert "Dissent" in out and "one member prefers Y" in out
        assert "failed: down" in out
