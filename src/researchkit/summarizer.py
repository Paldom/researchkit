"""Summarizer for consolidating insights from multiple providers."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

from researchkit.network_retry import with_network_retry
from researchkit.prompts import (
    get_meta_summary_system_prompt,
    get_meta_summary_user_prompt,
    get_single_summary_system_prompt,
    get_single_summary_user_prompt,
)
from researchkit.providers.base import ProviderResult
from researchkit.providers.glm_provider import is_glm_model
from researchkit.providers.kimi_provider import is_kimi_model

logger = logging.getLogger(__name__)


class Summarizer:
    """
    Uses Gemini to synthesize insights from multiple providers into a unified report.

    Provides both:
    - Individual summaries for each provider result (fallback only - providers now self-summarize)
    - A consolidated meta-summary across all providers
    """

    DEFAULT_MODEL = "gemini-3.5-flash"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize the summarizer.

        Args:
            api_key: Gemini API key (defaults to GEMINI_API_KEY env var)
            model: Model to use for summarization (defaults to gemini-3.5-flash)
        """
        self.api_key = (
            api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        )
        self.model = model or self.DEFAULT_MODEL
        # GLM models are served via the z.ai OpenAI-compatible endpoint rather
        # than the Gemini SDK, so callers can use GLM as the summarizer model.
        self._is_glm = is_glm_model(self.model)
        # Kimi models likewise route to the Moonshot OpenAI-compatible endpoint.
        self._is_kimi = is_kimi_model(self.model)
        # CLI-backed specs (codex:/agy:/grokcli:/claude*) route through the
        # logged-in harness instead of any API — the harness (subscription-only)
        # preset sets the summarizer slot to one of these.
        from researchkit.council import is_cli_backed_spec

        self._is_cli = is_cli_backed_spec(self.model)
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-load the summarization client (Gemini or GLM/z.ai; None for CLI)."""
        if self._is_cli:
            return None
        if self._client is None:
            if self._is_glm:
                from researchkit.providers.glm_provider import make_zai_client

                self._client = make_zai_client()
            elif self._is_kimi:
                from researchkit.providers.kimi_provider import make_kimi_client

                self._client = make_kimi_client()
            else:
                try:
                    from google import genai
                except ImportError as e:
                    raise RuntimeError(
                        "google-genai package not installed. Run: pip install google-genai"
                    ) from e
                if not self.api_key:
                    raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY not set")
                self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _generate(
        self,
        client: Any,
        prompt: str,
        *,
        label: str,
        temperature: float,
        max_output_tokens: int,
    ) -> str:
        """Run one completion against the configured backend (Gemini, GLM, or CLI)."""
        if self._is_cli:
            from researchkit.council import complete_via_spec

            # CLI harnesses take no temperature/token knobs; the prompt and the
            # harness's own defaults govern the output.
            return complete_via_spec(self.model, "", prompt, label=label)
        if self._is_glm:
            response = with_network_retry(
                client.chat.completions.create,
                label=label,
                provider="summarizer",
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_output_tokens,
            )
            return response.choices[0].message.content or ""
        if self._is_kimi:
            from researchkit.providers.kimi_provider import resolve_kimi_model

            # No temperature/max_tokens: Kimi models run fixed sampling params
            # (overrides rejected) and spend thinking tokens from the same
            # output budget, so a small cap truncates the summary.
            response = with_network_retry(
                client.chat.completions.create,
                label=label,
                provider="summarizer",
                model=resolve_kimi_model(self.model),
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or ""

        from google.genai import types

        response = with_network_retry(
            client.models.generate_content,
            label=label,
            provider="summarizer",
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            ),
        )
        return response.text or ""

    def summarize_single(self, result: ProviderResult) -> str:
        """
        Summarize a single provider's result into concise bullet points.

        Note: This is now primarily a fallback. Providers should self-summarize
        using their own summarize_result() method.

        Args:
            result: The provider result to summarize

        Returns:
            A summarized version of the result (5-8 bullet points)
        """
        logger.debug(
            f"Summarizing single result from {result.provider}",
            extra={"stage": "summarize_single_start", "provider": result.provider},
        )

        if result.error or not result.raw_text:
            logger.warning(
                f"Skipping summarization for {result.provider}: error or empty",
                extra={"stage": "summarize_single_skip", "provider": result.provider},
            )
            return f"*Error from {result.provider}: {result.error or 'No content'}*"

        try:
            client = self._get_client()
        except RuntimeError:
            logger.warning(
                "Gemini client unavailable, returning truncated content",
                extra={
                    "stage": "summarize_single_fallback",
                    "provider": result.provider,
                },
            )
            return (
                result.raw_text[:500] + "..."
                if len(result.raw_text) > 500
                else result.raw_text
            )

        system_prompt = get_single_summary_system_prompt()
        user_prompt = get_single_summary_user_prompt(
            result.provider, result.model, result.raw_text
        )

        try:
            prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
            summary = self._generate(
                client,
                prompt,
                label="summarizer.generate_content:single",
                temperature=0.3,
                max_output_tokens=1000,
            )
            logger.debug(
                f"Single summary complete for {result.provider}: {len(summary)} chars",
                extra={"stage": "summarize_single_done", "provider": result.provider},
            )
            return summary
        except Exception as e:
            logger.exception(
                f"Single summarization failed for {result.provider}",
                extra={"stage": "summarize_single_error", "provider": result.provider},
            )
            return f"*Summarization failed: {e}*\n\n{result.raw_text[:500]}..."

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

        try:
            client = self._get_client()
        except RuntimeError:
            logger.warning(
                "Gemini client unavailable, returning concatenated fallback",
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
        providers_data = [
            {
                "provider": r.provider,
                "model": r.model,
                "content": r.raw_text,
            }
            for r in successful_results
        ]
        uncited_providers = [r.provider for r in successful_results if not r.sources]

        system_prompt = get_meta_summary_system_prompt()
        user_prompt = get_meta_summary_user_prompt(
            topic=topic,
            days=days,
            providers_data=providers_data,
            successful_providers=[r.provider for r in successful_results],
            failed_providers=[r.provider for r in failed_results]
            if failed_results
            else None,
            uncited_providers=uncited_providers or None,
        )

        try:
            prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
            summary = self._generate(
                client,
                prompt,
                label="summarizer.generate_content:meta",
                temperature=0.4,
                # Thinking models (gemini-3.5-flash) spend reasoning tokens from
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
