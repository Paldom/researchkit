"""Summarizer for consolidating insights from multiple providers."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from researchkit.prompts import (
    get_meta_summary_system_prompt,
    get_meta_summary_user_prompt,
)
from researchkit.providers.base import ProviderResult

logger = logging.getLogger(__name__)


class Summarizer:
    """The cross-provider meta-summary, on a harness spec or a vendor API.

    Per-provider summaries are each provider's own ``summarize_result``.
    """

    DEFAULT_MODEL = "gemini-3.8-flash"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize the summarizer.

        Args:
            api_key: API key for the model's vendor (defaults to its env var)
            model: Model to use for summarization (defaults to gemini-3.8-flash)
        """
        # An explicit key overrides the vendor's env var; None reads the env.
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        # CLI-backed specs (codex:/agy:/grokcli:/kimicli:/claude*) route
        # through the logged-in harness instead of any API — the harness
        # (subscription-only) presets set the summarizer slot to one of these.
        # Plain ids go to whichever vendor serves them (api_llm, PLAN A.8).
        from researchkit.council import is_cli_backed_spec

        self._is_cli = is_cli_backed_spec(self.model)

    def _available(self) -> bool:
        """A harness spec, or an API id whose key is set (no request made)."""
        from researchkit.api_llm import has_key

        return self._is_cli or bool(self.api_key) or has_key(self.model)

    def _generate(
        self,
        prompt: str,
        *,
        label: str,
        temperature: float,
        max_output_tokens: int,
    ) -> str:
        """Run one completion on the configured backend (CLI or vendor API)."""
        if self._is_cli:
            from researchkit.council import complete_via_spec

            # CLI harnesses take no temperature/token knobs; the prompt and the
            # harness's own defaults govern the output.
            return complete_via_spec(self.model, "", prompt, label=label)
        from researchkit.api_llm import complete

        return complete(
            self.model,
            "",
            prompt,
            label=label,
            temperature=temperature,
            max_tokens=max_output_tokens,
            api_key=self.api_key,
        )

    def create_meta_summary(
        self,
        topic: str,
        days: int,
        provider_results: Sequence[ProviderResult],
    ) -> str:
        """
        Create a consolidated meta-summary from all provider results.

        Args:
            topic: The research topic
            days: The time window used
            provider_results: List of results from all providers

        Returns:
            A consolidated summary highlighting consensus and disagreements
        """
        logger.info(
            f"Creating meta-summary for {len(provider_results)} provider results",
            extra={"stage": "meta_summary_start"},
        )

        if not provider_results:
            logger.warning(
                "No provider results to summarize",
                extra={"stage": "meta_summary_empty"},
            )
            return "*No provider results to summarize.*"

        # Filter successful results
        successful_results = [r for r in provider_results if r.is_success]
        failed_results = [r for r in provider_results if not r.is_success]

        logger.debug(
            f"Meta-summary: {len(successful_results)} successful, {len(failed_results)} failed",
            extra={"stage": "meta_summary_filter"},
        )

        if not successful_results:
            logger.error(
                "All providers failed, cannot create meta-summary",
                extra={"stage": "meta_summary_all_failed"},
            )
            errors = "\n".join(f"- {r.provider}: {r.error}" for r in failed_results)
            return f"*All providers failed:*\n{errors}"

        if not self._available():
            logger.warning(
                f"No API key for summarizer model {self.model}, returning "
                "concatenated fallback",
                extra={"stage": "meta_summary_fallback"},
            )
            # Fallback: just concatenate summaries
            return "\n\n---\n\n".join(
                f"## {r.provider}\n{r.raw_text[:1000]}" for r in successful_results
            )

        # Build the providers data. A provider that returned prose but ZERO
        # extractable sources is flagged via the TRUSTED prompt channel
        # (uncited_providers below) — an in-band note inside the untrusted
        # block would be both ignorable and forgeable by injected content.
        # No model names: the report is about the topic, and a model id in the
        # context tends to resurface in the prose.
        providers_data = [
            {"provider": r.provider, "content": r.raw_text} for r in successful_results
        ]
        uncited_providers = [
            r.provider
            for r in successful_results
            if not r.sources and r.meta.get("evidence") != "procedures"
        ]

        system_prompt = get_meta_summary_system_prompt()
        user_prompt = get_meta_summary_user_prompt(
            topic=topic,
            days=days,
            providers_data=providers_data,
            uncited_providers=uncited_providers or None,
            procedure_providers=[
                r.provider
                for r in successful_results
                if r.meta.get("evidence") == "procedures"
            ]
            or None,
        )

        try:
            prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
            summary = self._generate(
                prompt,
                label="summarizer.generate_content:meta",
                temperature=0.4,
                # Thinking models (gemini-3.8-flash) spend reasoning tokens from
                # the same budget; 2500 left ~700 for text and truncated the
                # meta-summary mid-sentence (found via brain ingestion QA).
                max_output_tokens=8000,
            )
            logger.info(
                f"Meta-summary complete: {len(summary)} chars",
                extra={"stage": "meta_summary_done"},
            )
            return summary
        except Exception as e:
            logger.exception(
                "Meta-summarization failed",
                extra={"stage": "meta_summary_error"},
            )
            return f"*Meta-summarization failed: {e}*"
