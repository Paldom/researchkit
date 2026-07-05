"""Tests for the Claude provider's /deep-research mode."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from researchkit.providers.base import SourceType
from researchkit.providers.claude_provider import (
    ClaudeProvider,
    deep_research_underlying_model,
    is_deep_research_spec,
)


class TestSpecParsing:
    def test_bare_deep(self) -> None:
        assert is_deep_research_spec("deep")
        assert deep_research_underlying_model("deep") is None

    def test_deep_with_model(self) -> None:
        assert is_deep_research_spec("deep:claude-opus-4-8")
        assert (
            deep_research_underlying_model("deep:claude-opus-4-8") == "claude-opus-4-8"
        )

    def test_deep_research_alias(self) -> None:
        assert is_deep_research_spec("deep-research")
        assert is_deep_research_spec("Deep-Research:claude-sonnet-4-6")
        assert (
            deep_research_underlying_model("deep-research: claude-sonnet-4-6 ")
            == "claude-sonnet-4-6"
        )

    def test_plain_models_do_not_match(self) -> None:
        assert not is_deep_research_spec("claude-opus-4-8")
        assert not is_deep_research_spec("deepseek-v3")
        assert not is_deep_research_spec(None)
        assert not is_deep_research_spec("")

    def test_trailing_colon_means_default_model(self) -> None:
        assert is_deep_research_spec("deep:")
        assert deep_research_underlying_model("deep:") is None


class TestProviderInit:
    def test_deep_mode_with_model(self) -> None:
        p = ClaudeProvider(model="deep:claude-opus-4-8")
        assert p.deep_research
        assert p._cli_model == "claude-opus-4-8"
        assert p.model_name == "deep-research:claude-opus-4-8"
        assert p._model_args() == ["--model", "claude-opus-4-8"]

    def test_deep_mode_default_model(self) -> None:
        p = ClaudeProvider(model="deep")
        assert p.deep_research
        assert p._cli_model is None
        assert p.model_name == "deep-research"
        assert p._model_args() == []

    def test_plain_mode_unchanged(self) -> None:
        p = ClaudeProvider(model="claude-opus-4-8")
        assert not p.deep_research
        assert p._cli_model == "claude-opus-4-8"
        assert p.model_name == "claude-opus-4-8"
        assert p._model_args() == ["--model", "claude-opus-4-8"]


class TestDeepResearchPrompt:
    def test_includes_skill_topic_and_days(self) -> None:
        p = ClaudeProvider(model="deep")
        prompt = p._deep_research_prompt("ai agents", 7)
        assert prompt.startswith("/deep-research ")
        assert '"ai agents"' in prompt
        assert "last 7 days" in prompt

    def test_scope_follows_sources(self) -> None:
        social_only = ClaudeProvider(model="deep", sources={"social"})
        prompt = social_only._deep_research_prompt("x", 3)
        assert "social platforms" in prompt
        assert "broader web" not in prompt

        web_only = ClaudeProvider(model="deep", sources={"web"})
        prompt = web_only._deep_research_prompt("x", 3)
        assert "social platforms" not in prompt
        assert "broader web" in prompt


class TestUrlClassification:
    def test_social_and_web_domains(self) -> None:
        p = ClaudeProvider(model="deep")
        assert p._classify_url("https://reddit.com/r/foo") == SourceType.SOCIAL
        assert p._classify_url("https://www.reddit.com/r/foo") == SourceType.SOCIAL
        assert (
            p._classify_url("https://news.ycombinator.com/item?id=1")
            == SourceType.SOCIAL
        )
        assert p._classify_url("https://example.org/post") == SourceType.WEB
        # Suffix match must not treat lookalike hosts as social
        assert p._classify_url("https://notreddit.com/r/foo") == SourceType.WEB


class TestUrlExtraction:
    def test_markdown_citation_shapes(self) -> None:
        p = ClaudeProvider(model="deep")
        text = (
            "See [wiki](https://en.wikipedia.org/wiki/GPT-5_(model)) and "
            "**https://x.com/user/status/1** plus (https://example.com/a). "
            "Trailing dot https://example.com/b."
        )
        assert p._extract_urls(text) == [
            "https://en.wikipedia.org/wiki/GPT-5_(model)",
            "https://x.com/user/status/1",
            "https://example.com/a",
            "https://example.com/b",
        ]

    def test_dedup_preserves_order(self) -> None:
        p = ClaudeProvider(model="deep")
        text = "https://a.com/x then https://b.com/y then https://a.com/x"
        assert p._extract_urls(text) == ["https://a.com/x", "https://b.com/y"]


class TestFetchDeepResearch:
    def _completed(self, payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=json.dumps(payload), stderr=""
        )

    def test_single_run_with_expected_flags(self) -> None:
        p = ClaudeProvider(model="deep:claude-opus-4-8", max_budget=8.0)
        payload = {
            "result": (
                "Report body. See https://reddit.com/r/ai/comments/1 and "
                "https://techblog.example.com/post"
            ),
            "total_cost_usd": 2.5,
            "num_turns": 12,
        }
        with patch(
            "researchkit.providers.claude_provider._run_claude_cli_with_retry",
            return_value=self._completed(payload),
        ) as run:
            result = p.fetch_insights("ai agents", 7)

        assert run.call_count == 1
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("-p") + 1].startswith("/deep-research ")
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
        assert cmd[cmd.index("--max-budget-usd") + 1] == "8.0"
        # Effort defaults to medium: `max` fan-out is a deliberate choice.
        assert cmd[cmd.index("--effort") + 1] == "medium"
        # Safety-critical: bypassPermissions is only acceptable because the
        # run is pinned read-only.
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
        assert (
            cmd[cmd.index("--disallowed-tools") + 1] == "Write,Edit,NotebookEdit,Bash"
        )
        assert "--no-session-persistence" in cmd
        assert "--system-prompt" not in cmd
        assert "--allowed-tools" not in cmd
        assert run.call_args.kwargs["timeout"] == 2400
        # A 40-min run that hits the wall must not be replayed.
        assert run.call_args.kwargs["retry_on_timeout"] is False

        assert result.error is None
        assert result.model == "deep-research:claude-opus-4-8"
        assert result.meta["mode"] == "deep-research"
        assert result.meta["total_cost_usd"] == 2.5
        types = {s.url: s.source_type for s in result.sources}
        assert types["https://reddit.com/r/ai/comments/1"] == SourceType.SOCIAL
        assert types["https://techblog.example.com/post"] == SourceType.WEB

    def test_reasoning_effort_flows_through(self) -> None:
        p = ClaudeProvider(model="deep", reasoning_effort="high")
        with patch(
            "researchkit.providers.claude_provider._run_claude_cli_with_retry",
            return_value=self._completed({"result": "ok"}),
        ) as run:
            p.fetch_insights("x", 3)
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("--effort") + 1] == "high"
        assert "--model" not in cmd

    def test_cli_error_is_captured_not_raised(self) -> None:
        p = ClaudeProvider(model="deep")
        payload = {"is_error": True, "result": "budget exceeded"}
        with patch(
            "researchkit.providers.claude_provider._run_claude_cli_with_retry",
            return_value=self._completed(payload),
        ):
            result = p.fetch_insights("ai agents", 7)
        assert result.error is not None
        assert "budget exceeded" in result.error


class TestFinalSummaryUnwrapsDeepSpec:
    def test_deep_spec_unwraps_to_underlying_model(self) -> None:
        from types import SimpleNamespace

        from researchkit.final_summary import DigestGenerator

        em = SimpleNamespace(claude="deep:claude-opus-4-8", claude_max_budget=5.0)
        gen = DigestGenerator.from_effective_models(em)  # type: ignore[arg-type]
        assert gen.model == "claude-opus-4-8"
        assert gen.max_budget == 5.0

    def test_bare_deep_falls_back_to_default_model(self) -> None:
        from types import SimpleNamespace

        from researchkit.final_summary import DigestGenerator

        em = SimpleNamespace(claude="deep", claude_max_budget=5.0)
        gen = DigestGenerator.from_effective_models(em)  # type: ignore[arg-type]
        assert gen.model == "claude-sonnet-4-6"

    def test_plain_model_passes_through(self) -> None:
        from types import SimpleNamespace

        from researchkit.final_summary import DigestGenerator

        em = SimpleNamespace(claude="claude-opus-4-8", claude_max_budget=7.0)
        gen = DigestGenerator.from_effective_models(em)  # type: ignore[arg-type]
        assert gen.model == "claude-opus-4-8"
        assert gen.max_budget == 7.0
