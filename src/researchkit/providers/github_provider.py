"""GitHub provider combining GitHub REST API search with OpenAI site-restricted search."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from openai import OpenAI

from researchkit.council import is_cli_backed_spec
from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
    provider_http_timeout,
)

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubProvider(BaseProvider):
    """
    GitHub provider that searches for insights via two complementary strategies:

    1. **Keyword-based** — Uses user-supplied keywords (from the central keyword
       pipeline) to run multiple cheap GitHub REST API queries (repos + issues)
       per keyword. Falls back to using the topic when no keywords are provided.
    2. **Topic-based** — Uses OpenAI web search restricted to ``github.com``
       with the full topic for broader coverage (READMEs, wikis, discussions).

    Results from both strategies are merged (deduplicated by URL) and analyzed
    with the *improver* model to produce a relevance-filtered, categorized
    summary with links.
    """

    provider_name = "github"
    model_name = "github-search"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
        improver_model: str | None = None,
        keywords: list[str] | None = None,
    ) -> None:
        """
        Initialize the GitHub provider.

        Args:
            api_key: GitHub personal access token (defaults to GITHUB_TOKEN env var).
                     Optional — unauthenticated API allows 10 req/min.
            sources: Set of sources to query (unused, kept for interface compat).
            model: OpenAI model for site-restricted web search.
            improver_model: Model for relevance analysis and summary.
            keywords: User-supplied keywords from the central keyword pipeline.
                      If empty, the topic is used as the search query.
        """
        self.github_token = api_key or os.getenv("GITHUB_TOKEN", "")
        self.sources = sources or {"social", "web"}
        self.search_model = model or "gpt-5.5"
        self.improver_model = improver_model or model or "gpt-5.5"
        self.model_name = f"github-search + {self.improver_model}"
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.keywords = keywords or []
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        """Lazy-load the OpenAI client."""
        if self._client is None:
            # The site-restricted github.com web_search call routinely runs
            # ~112s (see provider_http_timeout docstring); a hardcoded 60s timed
            # out and silently dropped this whole phase. (Review M3.)
            self._client = OpenAI(
                api_key=self.openai_api_key,
                max_retries=0,
                timeout=provider_http_timeout(),
            )
        return self._client

    def _resolve_keywords(self, topic: str) -> list[str]:
        """Return keywords for GitHub API search.

        Uses user-supplied keywords from the central pipeline if available,
        otherwise falls back to the topic itself.
        """
        if self.keywords:
            return list(self.keywords)
        return [topic]

    # ------------------------------------------------------------------
    # GitHub API search (keyword-based)
    # ------------------------------------------------------------------

    def _github_headers(self) -> dict[str, str]:
        """Build headers for GitHub REST API requests."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    def _search_github_api(
        self,
        keywords: list[str],
        days: int,
    ) -> tuple[list[Source], list[dict[str, Any]]]:
        """
        Search GitHub REST API for repos and issues matching keywords.

        Returns:
            Tuple of (Source list, context dicts for analysis).
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        headers = self._github_headers()
        sources: list[Source] = []
        context_items: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for keyword in keywords:
            # Search repositories
            try:
                resp = with_network_retry(
                    requests.get,
                    f"{GITHUB_API_BASE}/search/repositories",
                    label="github.search.repositories",
                    provider=self.provider_name,
                    params={
                        "q": f"{keyword} pushed:>{cutoff}",
                        "sort": "updated",
                        "order": "desc",
                        "per_page": 15,
                    },
                    headers=headers,
                    timeout=(10, 30),
                )
                if resp.status_code == 200:
                    for repo in resp.json().get("items", []):
                        url = repo.get("html_url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            title = repo.get("full_name", "")
                            desc = repo.get("description") or ""
                            stars = repo.get("stargazers_count", 0)
                            sources.append(
                                Source(
                                    url=url,
                                    title=title,
                                    snippet=desc[:200] if desc else None,
                                    source_type=SourceType.SOCIAL,
                                )
                            )
                            context_items.append(
                                {
                                    "type": "repository",
                                    "url": url,
                                    "name": title,
                                    "description": desc[:300],
                                    "stars": stars,
                                    "keyword": keyword,
                                }
                            )
                else:
                    logger.debug(
                        f"GitHub repo search returned {resp.status_code} for '{keyword}'",
                        extra={"stage": "github_api", "provider": self.provider_name},
                    )
            except Exception as e:
                logger.warning(
                    f"GitHub repo search failed for '{keyword}': {e}",
                    extra={"stage": "github_api_error", "provider": self.provider_name},
                )

            # Search issues / PRs / discussions
            try:
                resp = with_network_retry(
                    requests.get,
                    f"{GITHUB_API_BASE}/search/issues",
                    label="github.search.issues",
                    provider=self.provider_name,
                    params={
                        "q": f"{keyword} created:>{cutoff}",
                        "sort": "updated",
                        "order": "desc",
                        "per_page": 15,
                    },
                    headers=headers,
                    timeout=(10, 30),
                )
                if resp.status_code == 200:
                    for issue in resp.json().get("items", []):
                        url = issue.get("html_url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            title = issue.get("title", "")
                            body = (issue.get("body") or "")[:200]
                            labels = [
                                lb.get("name", "") for lb in issue.get("labels", [])
                            ]
                            sources.append(
                                Source(
                                    url=url,
                                    title=title,
                                    snippet=body if body else None,
                                    source_type=SourceType.SOCIAL,
                                )
                            )
                            context_items.append(
                                {
                                    "type": "issue",
                                    "url": url,
                                    "title": title,
                                    "body_preview": body,
                                    "labels": labels[:5],
                                    "keyword": keyword,
                                }
                            )
                else:
                    logger.debug(
                        f"GitHub issue search returned {resp.status_code} for '{keyword}'",
                        extra={"stage": "github_api", "provider": self.provider_name},
                    )
            except Exception as e:
                logger.warning(
                    f"GitHub issue search failed for '{keyword}': {e}",
                    extra={"stage": "github_api_error", "provider": self.provider_name},
                )

        logger.info(
            f"GitHub API: {len(sources)} sources from {len(keywords)} keywords",
            extra={"stage": "github_api_done", "provider": self.provider_name},
        )
        return sources, context_items

    # ------------------------------------------------------------------
    # Phase 3: OpenAI site-restricted search on github.com
    # ------------------------------------------------------------------

    def _search_openai_github(
        self,
        topic: str,
        days: int,
    ) -> tuple[list[Source], str]:
        """
        Use OpenAI web search restricted to github.com for the full topic.

        Returns:
            Tuple of (Source list, raw response text).
        """
        if not self.openai_api_key:
            return [], ""

        try:
            client = self._get_client()

            system_prompt = (
                f"You are a GitHub research specialist. Search github.com thoroughly "
                f"for content related to the user's topic from the last {days} days.\n\n"
                f"Find and report on:\n"
                f"- Repositories (with star counts and descriptions)\n"
                f"- Issues and pull requests with active discussion\n"
                f"- README files and documentation\n"
                f"- GitHub Discussions threads\n"
                f"- GitHub blog posts and release notes\n\n"
                f"For each finding, provide: the title, URL, and a brief description "
                f"of why it is relevant. Organize results by category."
            )

            user_prompt = (
                f"Search GitHub.com for recent content related to:\n\n{topic}\n\n"
                f"Focus on the most actively discussed, starred, or recently updated "
                f"content from the last {days} days."
            )

            tool_config: dict[str, Any] = {
                "type": "web_search",
                "search_context_size": "high",
                "user_location": {"type": "approximate"},
                "filters": {"allowed_domains": ["github.com"]},
            }

            request_kwargs: dict[str, Any] = {
                "model": self.search_model,
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "tools": [tool_config],
                "store": False,
            }

            response = with_network_retry(
                client.responses.create,
                label="github.openai.responses.create",
                provider=self.provider_name,
                **request_kwargs,
            )

            # Extract text
            text = getattr(response, "output_text", None)
            if text is None and hasattr(response, "output") and response.output:
                for item in response.output:
                    if hasattr(item, "content") and item.content:
                        for content_item in item.content:
                            if hasattr(content_item, "text"):
                                text = content_item.text
                                break
                    if text:
                        break
            text = text or ""

            # Extract sources from annotations
            sources: list[Source] = []
            if hasattr(response, "output") and response.output:
                for item in response.output:
                    if hasattr(item, "content") and item.content:
                        for content_item in item.content:
                            if not (
                                hasattr(content_item, "annotations")
                                and content_item.annotations
                            ):
                                continue
                            for ann in content_item.annotations:
                                url = None
                                title = None
                                if hasattr(ann, "url_citation") and ann.url_citation:
                                    url = getattr(ann.url_citation, "url", None)
                                    title = getattr(ann.url_citation, "title", None)
                                elif hasattr(ann, "url") and ann.url:
                                    url = ann.url
                                    title = getattr(ann, "title", None)
                                if url:
                                    sources.append(
                                        Source(
                                            url=url,
                                            title=title,
                                            source_type=SourceType.WEB,
                                        )
                                    )

            logger.info(
                f"OpenAI github.com search: {len(sources)} sources, {len(text)} chars",
                extra={"stage": "github_openai_done", "provider": self.provider_name},
            )
            return sources, text

        except Exception as e:
            logger.warning(
                f"OpenAI github.com search failed: {e}",
                extra={"stage": "github_openai_error", "provider": self.provider_name},
            )
            return [], ""

    # ------------------------------------------------------------------
    # Phase 4: Merge, analyze, and summarize
    # ------------------------------------------------------------------

    def _merge_sources(
        self,
        api_sources: list[Source],
        openai_sources: list[Source],
    ) -> list[Source]:
        """Merge and deduplicate sources by normalized URL."""
        seen: dict[str, Source] = {}
        for source in [*api_sources, *openai_sources]:
            url = source.url.rstrip("/")
            # Prefer the version with more metadata
            if url not in seen or (source.title and not seen[url].title):
                seen[url] = source
        return list(seen.values())

    def _analyze_and_summarize(
        self,
        topic: str,
        days: int,
        sources: list[Source],
        api_context: list[dict[str, Any]],
        openai_text: str,
    ) -> str:
        """Use the improver model to analyze relevance and create a summary."""
        if not self.openai_api_key and not is_cli_backed_spec(self.improver_model):
            return openai_text or self._format_fallback(sources)

        # Build context from all collected data
        context_parts: list[str] = []

        if api_context:
            repos = [c for c in api_context if c["type"] == "repository"]
            issues = [c for c in api_context if c["type"] == "issue"]

            if repos:
                repo_lines = []
                for r in repos[:30]:
                    stars = r.get("stars", 0)
                    desc = r.get("description", "")[:150]
                    repo_lines.append(
                        f"- [{r['name']}]({r['url']}) ({stars}★) — {desc}"
                    )
                context_parts.append("## GitHub Repositories\n" + "\n".join(repo_lines))

            if issues:
                issue_lines = []
                for i in issues[:30]:
                    labels = ", ".join(i.get("labels", []))
                    label_str = f" [{labels}]" if labels else ""
                    issue_lines.append(f"- [{i['title']}]({i['url']}){label_str}")
                context_parts.append(
                    "## GitHub Issues & Discussions\n" + "\n".join(issue_lines)
                )

        if openai_text:
            context_parts.append(
                f"## OpenAI GitHub.com Search Results\n{openai_text[:4000]}"
            )

        # Additional URL-only sources not in context
        context_urls = {c.get("url", "") for c in api_context}
        extra_sources = [s for s in sources if s.url not in context_urls and s.title]
        if extra_sources:
            extra_lines = [f"- [{s.title}]({s.url})" for s in extra_sources[:20]]
            context_parts.append("## Additional Sources\n" + "\n".join(extra_lines))

        combined_context = "\n\n".join(context_parts)

        try:
            messages: list[dict[str, str]] = [
                {
                    "role": "system",
                    "content": (
                        "You are a GitHub research analyst. You receive raw search "
                        "results from GitHub (repositories, issues, discussions) and "
                        "a web search of github.com. Your job is to:\n\n"
                        "1. Filter out irrelevant results\n"
                        "2. Categorize the relevant ones into:\n"
                        "   - **Key Repositories** — most relevant/popular repos\n"
                        "   - **Notable Discussions & Issues** — active community threads\n"
                        "   - **Documentation & Guides** — READMEs, wikis, guides\n"
                        "   - **Related Projects** — tangentially relevant repos/tools\n"
                        "3. For each item write: [title](url) — brief description\n"
                        "4. End with a 3-5 sentence synthesis of what GitHub activity "
                        "reveals about this topic\n\n"
                        "Include ALL relevant links. Be thorough but skip clearly "
                        "irrelevant results. Use markdown formatting."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Analyze these GitHub search results for relevance to: "
                        f"{topic}\n"
                        f"Time window: last {days} days\n\n"
                        f"{combined_context}"
                    ),
                },
            ]
            if is_cli_backed_spec(self.improver_model):
                # improver slot may be a harness spec — run on the
                # logged-in CLI, no API key.
                from researchkit.council import complete_via_spec

                return complete_via_spec(
                    self.improver_model,
                    str(messages[0]["content"]),
                    str(messages[1]["content"]),
                    label="github.cli:analyze",
                )
            client = self._get_client()
            response = with_network_retry(
                client.chat.completions.create,
                label="github.openai.chat.completions:analyze",
                provider=self.provider_name,
                model=self.improver_model,
                messages=messages,
                max_completion_tokens=3000,
            )
            return response.choices[0].message.content or ""

        except Exception as e:
            logger.warning(
                f"Improver analysis failed: {e}",
                extra={
                    "stage": "github_analysis_error",
                    "provider": self.provider_name,
                },
            )
            return self._format_fallback(sources)

    def _format_fallback(self, sources: list[Source]) -> str:
        """Simple fallback formatting when analysis fails."""
        if not sources:
            return "No GitHub results found."
        lines = [f"- [{s.title or s.url}]({s.url})" for s in sources[:30]]
        return "## GitHub Sources\n\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch insights from GitHub API + OpenAI site search."""
        self._log_start()

        if not self.openai_api_key:
            return self._create_error_result(
                "OPENAI_API_KEY not set (required for GitHub provider)"
            )

        try:
            # Phase 1: Resolve keywords (user-supplied or topic fallback)
            keywords = self._resolve_keywords(topic)
            logger.info(
                f"GitHub search keywords: {keywords}",
                extra={"stage": "github_keywords", "provider": self.provider_name},
            )

            # Phase 2: GitHub API search (keyword-based)
            self._log_query("GitHub API")
            api_sources, api_context = self._search_github_api(keywords, days)

            # Phase 3: OpenAI site-restricted search (topic-based)
            self._log_query("OpenAI github.com")
            openai_sources, openai_text = self._search_openai_github(topic, days)

            # Phase 4: Merge, analyze, summarize
            self._log_query("analysis")
            all_sources = self._merge_sources(api_sources, openai_sources)
            summary = self._analyze_and_summarize(
                topic,
                days,
                all_sources,
                api_context,
                openai_text,
            )

            self._log_done(len(all_sources), len(summary))

            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=summary,
                sources=all_sources,
                meta={
                    "keywords_used": keywords,
                    "api_sources_count": len(api_sources),
                    "openai_sources_count": len(openai_sources),
                    "total_sources_count": len(all_sources),
                },
            )

        except Exception as e:
            return self._create_error_result(f"GitHub provider error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Summarize the GitHub analysis into key bullet points."""
        if not self.openai_api_key and not is_cli_backed_spec(self.improver_model):
            return raw_text[:500] + "..." if len(raw_text) > 500 else raw_text

        try:
            messages: list[dict[str, str]] = [
                {
                    "role": "system",
                    "content": (
                        "You are a precise summarizer. Distill GitHub research "
                        "reports into their essential points.\n\n"
                        "Rules:\n"
                        "- Extract 5-8 key bullet points\n"
                        "- Preserve repository names, URLs, and star counts\n"
                        "- Keep links in [title](url) format\n"
                        "- Be concise but preserve critical details"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Summarize this GitHub research report into 5-8 key "
                        f"bullet points:\n\n"
                        f"**Topic:** {topic}\n\n---\n{raw_text}\n---\n\n"
                        f"Format as a markdown bullet list. Start each bullet "
                        f"with a bold label when appropriate."
                    ),
                },
            ]
            if is_cli_backed_spec(self.improver_model):
                # improver slot may be a harness spec — run on the
                # logged-in CLI, no API key.
                from researchkit.council import complete_via_spec

                return complete_via_spec(
                    self.improver_model,
                    str(messages[0]["content"]),
                    str(messages[1]["content"]),
                    label="github.cli:summarize",
                )
            client = self._get_client()
            response = with_network_retry(
                client.chat.completions.create,
                label="github.openai.chat.completions:summarize",
                provider=self.provider_name,
                model=self.improver_model,
                messages=messages,
                max_completion_tokens=1000,
            )
            return response.choices[0].message.content or ""

        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
