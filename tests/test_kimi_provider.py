"""Tests for the Kimi API provider and the Kimi-Code-CLI-backed provider."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

import pytest

import researchkit.providers.kimicli_provider as kimicli_mod
from researchkit.providers import KimiCliProvider, KimiProvider
from researchkit.providers.base import SourceType
from researchkit.providers.kimi_provider import (
    get_kimi_api_key,
    is_kimi_model,
    kimi_base_url,
    make_kimi_client,
    resolve_kimi_model,
)
from researchkit.providers.kimicli_provider import (
    is_kimicli_model,
    kimicli_underlying_model,
)

# ---------------------------------------------------------------------------
# API provider
# ---------------------------------------------------------------------------


class TestKimiSpecHelpers:
    def test_is_kimi_model(self) -> None:
        assert is_kimi_model("kimi-k3")
        assert is_kimi_model("kimi-k2.6")
        assert is_kimi_model("KIMI-K2.6")
        assert is_kimi_model("moonshot-v1-8k")
        assert not is_kimi_model("kimicli")  # CLI spec, not an API id
        assert not is_kimi_model("kimicli:kimi-code/k3")
        assert not is_kimi_model("glm-5.2")
        assert not is_kimi_model(None)
        assert not is_kimi_model("")

    def test_get_kimi_api_key_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        assert get_kimi_api_key() is None
        monkeypatch.setenv("KIMI_API_KEY", "kk")
        assert get_kimi_api_key() == "kk"
        monkeypatch.setenv("MOONSHOT_API_KEY", "mk")  # official name wins
        assert get_kimi_api_key() == "mk"
        assert get_kimi_api_key("explicit") == "explicit"

    def test_make_client_requires_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_BASE_URL", raising=False)
        monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
            make_kimi_client()
        client = make_kimi_client("key")
        assert "api.moonshot.ai" in str(client.base_url)

    def test_base_url_override_and_model_dialect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIMI_BASE_URL", raising=False)
        monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
        assert "api.moonshot.ai" in kimi_base_url()
        # Open Platform: ids pass through untouched
        assert resolve_kimi_model("kimi-k2.6") == "kimi-k2.6"
        # Coding endpoint: Open-Platform-style ids translate to its dialect
        monkeypatch.setenv("KIMI_BASE_URL", "https://api.kimi.com/coding/v1")
        assert kimi_base_url() == "https://api.kimi.com/coding/v1"
        assert resolve_kimi_model("kimi-k3") == "k3"
        assert resolve_kimi_model("kimi-k2.6") == "kimi-for-coding"
        assert resolve_kimi_model("k3") == "k3"  # native ids pass through
        provider = KimiProvider(api_key="k", model="kimi-k3")
        assert provider.model_name == "k3"


@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    function: _FakeFunction


@dataclass
class _FakeMessage:
    content: str | None = None
    tool_calls: list[_FakeToolCall] = field(default_factory=list)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return {"role": "assistant", "content": self.content}


@dataclass
class _FakeChoice:
    finish_reason: str
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


def _tool_call_response() -> _FakeResponse:
    call = _FakeToolCall(
        id="c1", function=_FakeFunction("web_search", '{"query": "q"}')
    )
    return _FakeResponse(
        [_FakeChoice("tool_calls", _FakeMessage(content=None, tool_calls=[call]))]
    )


def _final_response(text: str) -> _FakeResponse:
    return _FakeResponse([_FakeChoice("stop", _FakeMessage(content=text))])


class TestKimiRunQuery:
    def test_web_search_round_trip_feeds_fiber_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k", model="kimi-k2.6")
        responses = [_tool_call_response(), _final_response("answer text")]
        seen_messages: list[list[dict[str, Any]]] = []
        fiber_calls: list[tuple[str, str]] = []

        def fake_chat(label: str, **kwargs: Any) -> _FakeResponse:
            seen_messages.append(list(kwargs["messages"]))
            return responses.pop(0)

        def fake_fiber(name: str, arguments: str, label: str) -> str:
            fiber_calls.append((name, arguments))
            return "----MOONSHOT ENCRYPTED----"

        monkeypatch.setattr(provider, "_chat", fake_chat)
        monkeypatch.setattr(provider, "_run_fiber", fake_fiber)
        text, searches = provider._run_query("sys", "user", label="t")
        assert text == "answer text"
        assert searches == 1
        assert fiber_calls == [("web_search", '{"query": "q"}')]
        # Second call carries the assistant tool-call message plus the tool
        # message whose content is the fiber's (encrypted) search output.
        second = seen_messages[1]
        assert second[-1]["role"] == "tool"
        assert second[-1]["tool_call_id"] == "c1"
        assert second[-1]["content"] == "----MOONSHOT ENCRYPTED----"

    def test_fiber_failure_degrades_that_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k")
        responses = [_tool_call_response(), _final_response("best effort")]
        seen_messages: list[list[dict[str, Any]]] = []

        def fake_chat(label: str, **kwargs: Any) -> _FakeResponse:
            seen_messages.append(list(kwargs["messages"]))
            return responses.pop(0)

        def broken_fiber(name: str, arguments: str, label: str) -> str:
            raise RuntimeError("404 no formulas here")

        monkeypatch.setattr(provider, "_chat", fake_chat)
        monkeypatch.setattr(provider, "_run_fiber", broken_fiber)
        text, searches = provider._run_query("sys", "user", label="t")
        assert text == "best effort"
        assert searches == 1  # the attempt still counts
        assert "web search unavailable" in seen_messages[1][-1]["content"]

    def test_empty_answer_after_search_gets_one_nudge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k")
        responses = [
            _tool_call_response(),
            _final_response(""),  # reasoning-only stop, observed live on k3
            _final_response("recovered answer"),
        ]
        nudges: list[list[dict[str, Any]]] = []

        def fake_chat(label: str, **kwargs: Any) -> _FakeResponse:
            nudges.append(list(kwargs["messages"]))
            return responses.pop(0)

        monkeypatch.setattr(provider, "_chat", fake_chat)
        text, searches = provider._run_query("sys", "user", label="t")
        assert text == "recovered answer"
        assert searches == 1
        assert nudges[-1][-1]["content"] == "Write the full answer now."

    def test_empty_answer_without_search_is_not_nudged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k")
        calls = {"n": 0}

        def fake_chat(label: str, **kwargs: Any) -> _FakeResponse:
            calls["n"] += 1
            return _final_response("")

        monkeypatch.setattr(provider, "_chat", fake_chat)
        assert provider._run_query("sys", "user", label="t") == ("", 0)
        assert calls["n"] == 1

    def test_round_cap_stops_a_looping_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k")
        calls = {"n": 0}

        def always_tool_call(label: str, **kwargs: Any) -> _FakeResponse:
            calls["n"] += 1
            return _tool_call_response()

        monkeypatch.setattr(provider, "_chat", always_tool_call)
        text, searches = provider._run_query("sys", "user", label="t")
        assert text == ""
        assert calls["n"] == 7  # _MAX_TOOL_ROUNDS + the one empty-answer nudge
        assert searches == 6


class TestKimiExtractSources:
    def test_markdown_links_keep_titles_and_bare_urls_dedup(self) -> None:
        provider = KimiProvider(api_key="k")
        text = (
            "See [The Story](https://example.test/story) and "
            "https://example.test/story plus https://example.test/other."
        )
        seen: set[str] = set()
        sources = provider._extract_sources(text, SourceType.WEB, seen)
        assert [(s.title, s.url) for s in sources] == [
            ("The Story", "https://example.test/story"),
            (None, "https://example.test/other"),
        ]
        # shared seen-set dedups across the social + web queries
        again = provider._extract_sources(text, SourceType.SOCIAL, seen)
        assert again == []


class TestKimiFetchInsights:
    def test_missing_key_is_graceful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        result = KimiProvider().fetch_insights("topic", days=7)
        assert not result.is_success
        assert "MOONSHOT_API_KEY" in (result.error or "")

    def test_dual_query_collects_sources_and_meta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k", model="kimi-k2.6")

        def fake_run_query(system_prompt: str, user_prompt: str, label: str):
            return "Found [x](https://example.test/x).", 2

        monkeypatch.setattr(provider, "_run_query", fake_run_query)
        result = provider.fetch_insights("ai agents", days=7)
        assert result.is_success
        assert result.provider == "kimi"
        assert result.model == "kimi-k2.6"
        assert {s.url for s in result.sources} == {"https://example.test/x"}
        assert result.meta == {"social_searches": 2, "web_searches": 2}
        assert "Social Media Analysis" in result.raw_text
        assert "Web Research Analysis" in result.raw_text

    def test_api_error_becomes_error_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k")

        def boom(system_prompt: str, user_prompt: str, label: str):
            raise RuntimeError("rate limit")

        monkeypatch.setattr(provider, "_run_query", boom)
        result = provider.fetch_insights("topic", days=7)
        assert not result.is_success
        assert "rate limit" in (result.error or "")


class TestKimiKeywordsAndSummary:
    def test_generate_keywords_parses_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k")
        payload = json.dumps({"keywords": ["agent memory design", "crypto ai signals"]})
        monkeypatch.setattr(
            provider, "_chat", lambda label, **kw: _final_response(payload)
        )
        assert provider.generate_keywords("topic", 7) == [
            "agent memory design",
            "crypto ai signals",
        ]

    def test_generate_keywords_without_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        assert KimiProvider().generate_keywords("topic", 7) == []

    def test_summarize_result_and_fallbacks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiProvider(api_key="k")
        monkeypatch.setattr(
            provider, "_chat", lambda label, **kw: _final_response("- bullet")
        )
        assert provider.summarize_result("raw", "topic") == "- bullet"

        def boom(label: str, **kw: Any):
            raise RuntimeError("down")

        monkeypatch.setattr(provider, "_chat", boom)
        assert "Summarization failed" in provider.summarize_result("raw " * 200, "t")

        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        keyless = KimiProvider()
        assert keyless.summarize_result("short", "t") == "short"


# ---------------------------------------------------------------------------
# CLI provider
# ---------------------------------------------------------------------------


class TestKimicliSpecHelpers:
    def test_is_kimicli_model(self) -> None:
        assert is_kimicli_model("kimicli")
        assert is_kimicli_model("kimicli:kimi-code/k3")
        assert is_kimicli_model("KIMICLI:kimi-code/k3")
        assert not is_kimicli_model("kimi-k2.6")  # plain id -> API path
        assert not is_kimicli_model("codex:gpt-5.5")
        assert not is_kimicli_model(None)
        assert not is_kimicli_model("")

    def test_underlying_model(self) -> None:
        assert kimicli_underlying_model("kimicli:kimi-code/k3") == "kimi-code/k3"
        assert kimicli_underlying_model("kimicli") is None
        assert kimicli_underlying_model("kimicli:") is None
        assert kimicli_underlying_model(None) is None


class TestKimicliBuildCmd:
    def test_command_shape(self) -> None:
        provider = KimiCliProvider(model="kimi-code/k3")
        cmd = provider._build_cmd(web_search=True)
        assert cmd[0] == "kimi"
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert cmd[cmd.index("-m") + 1] == "kimi-code/k3"

    def test_default_model_omits_flag(self) -> None:
        assert "-m" not in KimiCliProvider()._build_cmd(web_search=False)

    def test_model_label(self) -> None:
        assert (
            KimiCliProvider(model="kimi-code/k3")._model_label == "kimicli:kimi-code/k3"
        )
        assert KimiCliProvider()._model_label == "kimicli:default"
        assert KimiCliProvider().provider_name == "kimi"


class TestKimicliParseOutput:
    def test_last_assistant_message_wins(self) -> None:
        out = "\n".join(
            [
                json.dumps({"role": "assistant", "content": "thinking aloud"}),
                json.dumps({"role": "tool", "content": "tool result"}),
                json.dumps({"role": "assistant", "content": "final answer"}),
                json.dumps({"role": "meta", "type": "session.resume_hint"}),
            ]
        )
        assert KimiCliProvider._parse_output(out) == ("final answer", [])

    def test_error_meta_raises(self) -> None:
        out = json.dumps({"role": "meta", "type": "error", "content": "rate limit"})
        with pytest.raises(RuntimeError, match="rate limit"):
            KimiCliProvider._parse_output(out)

    def test_garbage_lines_skipped(self) -> None:
        assert KimiCliProvider._parse_output("not json\n\n[1,2]\n") == ("", [])

    def test_empty_output(self) -> None:
        assert KimiCliProvider._parse_output("") == ("", [])


def _fake_run(text: str):
    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Prompt rides argv after -p; neutral temp cwd keeps the host project
        # out of the CLI's workspace.
        assert cmd[cmd.index("-p") + 1]
        assert kwargs.get("cwd") == tempfile.gettempdir()
        assert "MOONSHOT_API_KEY" not in kwargs.get("env", {})
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"role": "assistant", "content": text}),
            stderr="",
        )

    return run


class TestKimicliFetchInsights:
    def test_dual_query_extracts_inline_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KimiCliProvider(model="kimi-code/k3")
        monkeypatch.setenv("MOONSHOT_API_KEY", "secret")  # must be scrubbed
        monkeypatch.setattr(
            "researchkit.providers.codex_provider.shutil.which", lambda _: "/bin/kimi"
        )
        monkeypatch.setattr(
            kimicli_mod,
            "run_subprocess",
            _fake_run("Found [thing](https://example.test/story). More prose."),
        )
        monkeypatch.setattr(KimiCliProvider, "_backfill_titles", lambda self, s: None)

        result = provider.fetch_insights("ai agents", days=7)
        assert result.is_success
        assert result.provider == "kimi"
        assert result.model == "kimicli:kimi-code/k3"
        assert {s.url for s in result.sources} == {"https://example.test/story"}
        assert "Social Media Analysis" in result.raw_text
        assert "Web Research Analysis" in result.raw_text

    def test_missing_binary_is_graceful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "researchkit.providers.codex_provider.shutil.which", lambda _: None
        )
        result = KimiCliProvider().fetch_insights("topic", days=7)
        assert not result.is_success
        assert "not found on PATH" in (result.error or "")

    def test_nonzero_exit_raises_inside_retried_callable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raised: list[RuntimeError] = []

        def fake_retry(fn: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return fn()
            except RuntimeError as e:
                raised.append(e)
                raise

        monkeypatch.setattr(kimicli_mod, "with_network_retry", fake_retry)

        def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="quota hit")

        monkeypatch.setattr(kimicli_mod, "run_subprocess", run)
        with pytest.raises(RuntimeError, match="quota hit"):
            KimiCliProvider()._exec("prompt", web_search=False, label="t")
        assert len(raised) == 1


class TestKimiRouting:
    def test_factory_routes_kimicli_spec(self) -> None:
        from researchkit.plugin_api import ProviderContext
        from researchkit.plugins_builtin import _make_kimi

        ctx = ProviderContext(
            model="kimicli:kimi-code/k3",
            sources=frozenset({"web"}),
            keywords=(),
            options={"reasoning_effort": "low"},
        )
        provider = _make_kimi(ctx)
        assert isinstance(provider, KimiCliProvider)
        assert provider.model_name == "kimi-code/k3"
        assert provider.provider_name == "kimi"

    def test_factory_routes_plain_id_to_api(self) -> None:
        from researchkit.plugin_api import ProviderContext
        from researchkit.plugins_builtin import _make_kimi

        ctx = ProviderContext(
            model="kimi-k2.6", sources=frozenset({"web"}), keywords=(), options={}
        )
        provider = _make_kimi(ctx)
        assert isinstance(provider, KimiProvider)
        assert not isinstance(provider, KimiCliProvider)

    def test_council_routes_kimicli_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchkit.council import LLMCouncil

        calls: list[str] = []

        def fake_exec(self, prompt: str, *, web_search: bool, label: str):
            calls.append(self.model_name)
            assert web_search is False
            return json.dumps({"improved_topic": "better topic"}), []

        monkeypatch.setattr(KimiCliProvider, "_exec", fake_exec)
        council = LLMCouncil(members=["kimicli:kimi-code/k3"], boss="kimicli")
        text = council._complete("kimicli:kimi-code/k3", "sys", "user", label="t")
        assert "better topic" in text
        assert calls == ["kimi-code/k3"]

    def test_improver_routes_kimi_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from researchkit.prompt_improver import PromptImprover
        from researchkit.system_config import SystemConfigManager

        real = SystemConfigManager()

        class KimiMgr:
            def resolve_effective_models(self, name=None):
                from dataclasses import replace

                return replace(
                    real.resolve_effective_models("default"), improver="kimi-k2.6"
                )

        imp = PromptImprover.from_system_config(KimiMgr())  # type: ignore[arg-type]
        assert imp.provider == "kimi"
        assert imp.model == "kimi-k2.6"

    def test_summarizer_uses_kimi_client(self) -> None:
        from researchkit.summarizer import Summarizer

        s = Summarizer(model="kimi-k2.6", api_key="unused")
        assert s._is_kimi and not s._is_cli
        s_cli = Summarizer(model="kimicli:kimi-code/k3")
        assert s_cli._is_cli and not s_cli._is_kimi
