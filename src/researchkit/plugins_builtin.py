"""Built-in providers and connectors, registered as default plugins.

Built-ins never come from entry points: registering them in code means core
works with zero installed metadata and their names can never be shadowed by
an external plugin. Each factory adapts the uniform
:class:`~researchkit.plugin_api.ProviderContext` back onto the concrete
constructor it wraps (the legacy preset knobs arrive in ``ctx.options`` —
see ``aggregator``'s context construction).
"""

from __future__ import annotations

from researchkit.plugin_api import (
    BaseProvider,
    BaseSiteConnector,
    ConnectorContext,
    ConnectorSpec,
    ProviderContext,
    ProviderSpec,
)


def _sources(ctx: ProviderContext) -> set[str]:
    return set(ctx.sources)


def _make_openai(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers import (
        CodexProvider,
        OpenAIProvider,
        codex_underlying_model,
        is_codex_model,
    )

    # `codex` / `codex:<model>` routes the OpenAI slot through the Codex CLI;
    # it reports as "openai" so it drops into the existing pipeline.
    if is_codex_model(ctx.model):
        return CodexProvider(
            sources=_sources(ctx),
            model=codex_underlying_model(ctx.model),
            reasoning_effort=str(ctx.options.get("reasoning_effort", "medium")),
            provider_name="openai",
        )
    return OpenAIProvider(
        sources=_sources(ctx),
        model=ctx.model or None,
        reasoning_effort=str(ctx.options.get("reasoning_effort", "medium")),
    )


def _make_gemini(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers import (
        AntigravityProvider,
        GeminiProvider,
        antigravity_underlying_model,
        is_antigravity_model,
    )

    # `agy` / `agy:<model>` routes the Gemini slot through the Antigravity
    # CLI; it reports as "gemini".
    if is_antigravity_model(ctx.model):
        return AntigravityProvider(
            sources=_sources(ctx),
            model=antigravity_underlying_model(ctx.model),
            provider_name="gemini",
        )
    return GeminiProvider(sources=_sources(ctx), model=ctx.model or None)


def _make_grok(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers import (
        GrokCliProvider,
        GrokProvider,
        grokcli_underlying_model,
        is_grokcli_model,
    )

    # `grokcli` / `grokcli:<model>` routes the grok slot through the Grok
    # CLI's headless mode (grok.com subscription auth); it reports as "grok".
    if is_grokcli_model(ctx.model):
        return GrokCliProvider(
            sources=_sources(ctx),
            model=grokcli_underlying_model(ctx.model),
            reasoning_effort=str(ctx.options.get("reasoning_effort", "medium")),
            provider_name="grok",
        )
    return GrokProvider(sources=_sources(ctx), model=ctx.model or None)


def _make_perplexity(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers import PerplexityProvider

    return PerplexityProvider(
        sources=_sources(ctx),
        model=ctx.model or None,
        search_type=str(ctx.options.get("search_type", "fast")),
    )


def _make_tavily(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers import TavilyProvider

    return TavilyProvider(
        sources=_sources(ctx),
        model=ctx.model or None,
        search_depth=str(ctx.options.get("search_depth", "fast")),
    )


def _make_claude(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers import (
        ClaudeProvider,
        claude_cli_underlying_model,
        is_claude_cli_spec,
    )

    # Canonical `claude:<model>` spec unwraps to the underlying model; bare
    # ids and `deep:<model>` pass through (the provider handles deep specs).
    model: str | None = ctx.model
    if is_claude_cli_spec(model):
        model = claude_cli_underlying_model(model)
    return ClaudeProvider(
        sources=_sources(ctx),
        model=model or None,
        max_budget=float(ctx.options.get("max_budget", 5.0)),
        reasoning_effort=str(ctx.options.get("reasoning_effort", "medium")),
    )


def _make_github(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers import GitHubProvider

    return GitHubProvider(
        sources=_sources(ctx),
        model=ctx.model or None,
        improver_model=str(ctx.options.get("improver_model", "")) or None,
        keywords=list(ctx.keywords),
    )


def _make_glm(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers import GLMProvider

    return GLMProvider(sources=_sources(ctx), model=ctx.model or None)


def _make_exa_provider(ctx: ProviderContext) -> BaseProvider:
    from researchkit.providers.exa_provider import ExaProvider

    return ExaProvider(
        sources=_sources(ctx),
        model=ctx.model or None,
        num_results=int(ctx.options.get("num_results", 20)),
    )


def _make_exa(ctx: ConnectorContext) -> BaseSiteConnector:
    from researchkit.site_research.connectors.exa import ExaConnector

    kwargs: dict[str, object] = {"gemini_model": ctx.summarizer_model}
    for key in (
        "search_type",
        "num_results",
        "include_context",
        "text_max_characters",
        "highlights_per_url",
    ):
        if key in ctx.options:
            kwargs[key] = ctx.options[key]
    return ExaConnector(**kwargs)  # type: ignore[arg-type]


def builtin_providers() -> tuple[ProviderSpec, ...]:
    """The default provider plugins, in pipeline order."""
    return (
        ProviderSpec("openai", _make_openai, is_llm=True, supports_improver=True),
        ProviderSpec("gemini", _make_gemini, is_llm=True, supports_improver=True),
        ProviderSpec("grok", _make_grok, is_llm=True, supports_improver=True),
        ProviderSpec(
            "perplexity", _make_perplexity, is_llm=False, supports_improver=True
        ),
        ProviderSpec("tavily", _make_tavily),
        ProviderSpec("claude", _make_claude, is_llm=True),
        ProviderSpec("github", _make_github),
        ProviderSpec("glm", _make_glm, is_llm=True, supports_improver=True),
        # Exa is a first-class provider (like tavily) AND powers the exa
        # site-research connector below — independent registrations.
        ProviderSpec("exa", _make_exa_provider, requires_env=("EXA_API_KEY",)),
    )


def builtin_connectors() -> tuple[ConnectorSpec, ...]:
    """The default connector plugins."""
    return (ConnectorSpec("exa", _make_exa, requires_env=("EXA_API_KEY",)),)
