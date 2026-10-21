"""PLAN A.8: one dispatcher for plain-API completions, knobs per vendor."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from researchkit import api_llm


class _FakeChat:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return NS(choices=[NS(message=NS(content="ok"))])


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("gpt-5.4-mini", "openai"),
        ("gemini-3.8-flash", "gemini"),
        ("grok-4.7", "grok"),
        ("sonar-pro", "perplexity"),
        ("glm-5.3", "glm"),
        ("kimi-k3", "kimi"),
        ("moonshot-v1", "kimi"),
    ],
)
def test_provider_for(model: str, provider: str) -> None:
    assert api_llm.provider_for(model) == provider


@pytest.mark.parametrize(
    ("model", "expect_temp", "expect_max"),
    [("gpt-5.4-mini", False, False), ("grok-4.7", True, True), ("glm-5.3", True, True)],
)
def test_openai_compatible_knobs(
    monkeypatch: pytest.MonkeyPatch, model: str, expect_temp: bool, expect_max: bool
) -> None:
    fake = _FakeChat()
    monkeypatch.setattr(api_llm, "_openai_compatible", lambda provider, key: fake)
    out = api_llm.complete(
        model, "sys", "user", label="t", json_mode=True, temperature=0.3, max_tokens=50
    )
    assert out == "ok"
    assert fake.kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert fake.kwargs["response_format"] == {"type": "json_object"}
    assert ("temperature" in fake.kwargs) is expect_temp
    assert ("max_tokens" in fake.kwargs) is expect_max


def test_kimi_sends_no_sampling_overrides_and_resolves_the_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeChat()
    monkeypatch.setattr(api_llm, "_openai_compatible", lambda provider, key: fake)
    api_llm.complete("kimi-k3", "", "user", label="t", temperature=0.3, max_tokens=50)
    assert "temperature" not in fake.kwargs and "max_tokens" not in fake.kwargs
    assert fake.kwargs["messages"] == [{"role": "user", "content": "user"}]
    assert "response_format" not in fake.kwargs  # json_mode off


def test_gemini_folds_the_system_prompt_and_sets_json_mime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google import genai

    seen: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, api_key: str) -> None:
            seen["key"] = api_key
            self.models = NS(generate_content=self._gen)

        def _gen(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return NS(text="g")

    monkeypatch.setattr(genai, "Client", FakeClient)
    out = api_llm.complete(
        "gemini-3.8-flash", "sys", "user", label="t", json_mode=True, api_key="k"
    )
    assert out == "g" and seen["key"] == "k"
    assert seen["contents"] == "sys\n\n---\n\nuser"
    assert seen["config"].response_mime_type == "application/json"


def test_missing_key_is_reported_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert not api_llm.has_key("gpt-5.4-mini")
    monkeypatch.setenv("XAI_API_KEY", "x")
    assert api_llm.has_key("grok-4.7")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY not set"):
        api_llm.complete("gpt-5.4-mini", "", "u", label="t")


def test_improver_and_synthesizer_route_through_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchkit.keyword_synthesizer import KeywordSynthesizer
    from researchkit.prompt_improver import PromptImprover

    calls: list[dict[str, Any]] = []

    def fake_complete(model: str, system: str, user: str, **kw: Any) -> str:
        calls.append({"model": model, **kw})
        return '{"keywords": ["a"]}'

    monkeypatch.setattr(api_llm, "complete", fake_complete)
    PromptImprover(provider="perplexity", model="sonar")._call_provider("s", "u")
    KeywordSynthesizer(model="glm-5.3")._complete("s", "u")
    assert calls[0]["provider"] == "perplexity" and calls[0]["json_mode"] is False
    # A GLM improver no longer gets forced onto the OpenAI client.
    assert calls[1]["model"] == "glm-5.3" and calls[1]["json_mode"] is True
