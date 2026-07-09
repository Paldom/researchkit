"""Provider implementations for social insight collection."""

from researchkit.providers.antigravity_provider import (
    AntigravityProvider,
    antigravity_underlying_model,
    is_antigravity_model,
)
from researchkit.providers.base import BaseProvider, ProviderResult, Source, SourceType
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
from researchkit.providers.gemini_provider import GeminiProvider
from researchkit.providers.github_provider import GitHubProvider
from researchkit.providers.glm_provider import GLMProvider
from researchkit.providers.grok_provider import GrokProvider
from researchkit.providers.grokcli_provider import (
    GrokCliProvider,
    grokcli_underlying_model,
    is_grokcli_model,
)
from researchkit.providers.openai_provider import OpenAIProvider
from researchkit.providers.perplexity_provider import PerplexityProvider
from researchkit.providers.tavily_provider import TavilyProvider

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
]
