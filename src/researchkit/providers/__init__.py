"""Provider implementations for social insight collection.

Exports resolve lazily (PEP 562): importing the package, or
``researchkit.providers.base``, no longer loads every provider module and
its SDK (google-genai alone cost ~0.6 s per CLI invocation). A name loads
its module on first access.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from researchkit.providers.antigravity_provider import (
        AntigravityProvider,
        antigravity_underlying_model,
        is_antigravity_model,
    )
    from researchkit.providers.base import (
        BaseProvider,
        ProviderResult,
        Source,
        SourceType,
    )
    from researchkit.providers.claude_provider import (
        ClaudeProvider,
        claude_cli_underlying_model,
        deep_research_underlying_model,
        is_claude_cli_spec,
        is_deep_research_spec,
    )
    from researchkit.providers.codex_provider import (
        CodexProvider,
        codex_underlying_model,
        is_codex_model,
    )
    from researchkit.providers.gemini_provider import (
        GeminiProvider,
    )
    from researchkit.providers.github_provider import (
        GitHubProvider,
    )
    from researchkit.providers.glm_provider import (
        GLMProvider,
    )
    from researchkit.providers.grok_provider import (
        GrokProvider,
    )
    from researchkit.providers.grokcli_provider import (
        GrokCliProvider,
        grokcli_underlying_model,
        is_grokcli_model,
    )
    from researchkit.providers.kimi_provider import (
        KimiProvider,
        is_kimi_model,
    )
    from researchkit.providers.kimicli_provider import (
        KimiCliProvider,
        is_kimicli_model,
        kimicli_underlying_model,
    )
    from researchkit.providers.openai_provider import (
        OpenAIProvider,
    )
    from researchkit.providers.perplexity_provider import (
        PerplexityProvider,
    )
    from researchkit.providers.tavily_provider import (
        TavilyProvider,
    )

_EXPORTS: dict[str, str] = {
    "AntigravityProvider": "researchkit.providers.antigravity_provider",
    "antigravity_underlying_model": "researchkit.providers.antigravity_provider",
    "is_antigravity_model": "researchkit.providers.antigravity_provider",
    "BaseProvider": "researchkit.providers.base",
    "ProviderResult": "researchkit.providers.base",
    "Source": "researchkit.providers.base",
    "SourceType": "researchkit.providers.base",
    "ClaudeProvider": "researchkit.providers.claude_provider",
    "claude_cli_underlying_model": "researchkit.providers.claude_provider",
    "deep_research_underlying_model": "researchkit.providers.claude_provider",
    "is_claude_cli_spec": "researchkit.providers.claude_provider",
    "is_deep_research_spec": "researchkit.providers.claude_provider",
    "CodexProvider": "researchkit.providers.codex_provider",
    "codex_underlying_model": "researchkit.providers.codex_provider",
    "is_codex_model": "researchkit.providers.codex_provider",
    "GeminiProvider": "researchkit.providers.gemini_provider",
    "GitHubProvider": "researchkit.providers.github_provider",
    "GLMProvider": "researchkit.providers.glm_provider",
    "GrokProvider": "researchkit.providers.grok_provider",
    "GrokCliProvider": "researchkit.providers.grokcli_provider",
    "grokcli_underlying_model": "researchkit.providers.grokcli_provider",
    "is_grokcli_model": "researchkit.providers.grokcli_provider",
    "KimiProvider": "researchkit.providers.kimi_provider",
    "is_kimi_model": "researchkit.providers.kimi_provider",
    "KimiCliProvider": "researchkit.providers.kimicli_provider",
    "is_kimicli_model": "researchkit.providers.kimicli_provider",
    "kimicli_underlying_model": "researchkit.providers.kimicli_provider",
    "OpenAIProvider": "researchkit.providers.openai_provider",
    "PerplexityProvider": "researchkit.providers.perplexity_provider",
    "TavilyProvider": "researchkit.providers.tavily_provider",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # cache: later lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTS])


__all__ = [
    "AntigravityProvider",
    "BaseProvider",
    "ClaudeProvider",
    "CodexProvider",
    "GLMProvider",
    "GeminiProvider",
    "GitHubProvider",
    "GrokCliProvider",
    "GrokProvider",
    "KimiCliProvider",
    "KimiProvider",
    "OpenAIProvider",
    "PerplexityProvider",
    "ProviderResult",
    "Source",
    "SourceType",
    "TavilyProvider",
    "antigravity_underlying_model",
    "claude_cli_underlying_model",
    "codex_underlying_model",
    "deep_research_underlying_model",
    "grokcli_underlying_model",
    "is_antigravity_model",
    "is_claude_cli_spec",
    "is_codex_model",
    "is_deep_research_spec",
    "is_grokcli_model",
    "is_kimi_model",
    "is_kimicli_model",
    "kimicli_underlying_model",
]
