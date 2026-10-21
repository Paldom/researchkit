"""One dispatcher for plain-API (non-search) completions (PLAN A.8).

The improver, the keyword synthesizer, the summarizer and every
``complete_via_spec`` call with a plain model id used to build their own
clients, with their own retry policy (or none) and their own idea of which
id goes to which vendor. They all route here now, so a fix lands once.
CLI-backed specs never reach this module: ``council.complete_via_spec``
sends them to the logged-in harness first.

Each backend keeps the knobs it already had: ``temperature`` only where the
vendor accepts it (Gemini, Grok, GLM; current OpenAI reasoning models and
Kimi reject overrides, Perplexity ignores them), JSON mode only when the
caller asks for it.
"""

from __future__ import annotations

import os
from typing import Any

from researchkit.network_retry import with_network_retry

PROVIDERS = ("openai", "gemini", "grok", "perplexity", "glm", "kimi")


def provider_for(model: str) -> str:
    """Which vendor API serves a plain model id."""
    m = model.lower()
    if m.startswith(("gemini", "models/gemini")):
        return "gemini"
    if m.startswith("grok"):
        return "grok"
    if m.startswith("sonar"):
        return "perplexity"
    if m.startswith("glm"):
        return "glm"
    if m.startswith(("kimi", "moonshot")):
        return "kimi"
    return "openai"


_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "grok": ("XAI_API_KEY",),
    "perplexity": ("PERPLEXITY_API_KEY",),
    "glm": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY"),  # = get_zai_api_key
    "kimi": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
}


def has_key(model: str, provider: str | None = None) -> bool:
    """Whether the vendor's API key is set (no request is made)."""
    return any(os.getenv(k) for k in _KEYS[provider or provider_for(model)])


def _key(*names: str) -> str:
    for name in names:
        if value := os.getenv(name):
            return value
    raise RuntimeError(f"{' or '.join(names)} not set")


def _openai_compatible(provider: str, api_key: str | None) -> Any:
    if provider == "glm":
        from researchkit.providers.glm_provider import make_zai_client

        return make_zai_client(api_key)
    if provider == "kimi":
        from researchkit.providers.kimi_provider import make_kimi_client

        return make_kimi_client(api_key)
    from openai import OpenAI

    if provider == "grok":
        return OpenAI(
            api_key=api_key or _key(*_KEYS["grok"]), base_url="https://api.x.ai/v1"
        )
    return OpenAI(api_key=api_key or _key(*_KEYS["openai"]))


def complete(
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    label: str,
    provider: str | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    api_key: str | None = None,
) -> str:
    """Run one completion on the vendor API ``model`` belongs to.

    ponytail: a fresh client per call (construction is cheap next to the
    request); cache per provider if a hot loop ever shows up.
    """
    provider = provider or provider_for(model)
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")

    if provider == "gemini":
        from google import genai
        from google.genai import types

        gemini = genai.Client(api_key=api_key or _key(*_KEYS["gemini"]))
        prompt = (
            f"{system_prompt}\n\n---\n\n{user_prompt}" if system_prompt else user_prompt
        )
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_mode else None,
        )
        response = with_network_retry(
            gemini.models.generate_content,
            label=label,
            provider=provider,
            model=model,
            contents=prompt,
            config=config,
        )
        return str(response.text or "")

    messages = [{"role": "user", "content": user_prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    client: Any

    if provider == "perplexity":
        from perplexity import Perplexity

        key = api_key or os.getenv("PERPLEXITY_API_KEY")
        client = Perplexity(api_key=key) if key else Perplexity()
        kwargs["stream"] = False
    else:
        client = _openai_compatible(provider, api_key)
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if provider == "kimi":
            from researchkit.providers.kimi_provider import resolve_kimi_model

            # Fixed sampling, and thinking tokens share the output budget:
            # neither temperature nor a small max_tokens may be sent.
            kwargs["model"] = resolve_kimi_model(model)
        elif provider in ("grok", "glm"):
            if temperature is not None:
                kwargs["temperature"] = temperature
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens

    response = with_network_retry(
        client.chat.completions.create, label=label, provider=provider, **kwargs
    )
    return str(response.choices[0].message.content or "")
