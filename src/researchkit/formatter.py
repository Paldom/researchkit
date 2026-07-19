"""Formatter for outputting insight reports in various formats."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from researchkit.aggregator import InsightBundle
    from researchkit.project import UserFileSource, UserUrlSource
    from researchkit.providers.base import ProviderResult, Source
    from researchkit.site_research import SiteResearchBundle


def _md_link(label: str | None, url: str | None) -> str:
    """Build a markdown link that survives ``]``/``)`` in untrusted title/url.

    A ``]`` in the label or a ``)`` in the URL would otherwise break the link or
    redirect it to an attacker-chosen target when the report is rendered. The
    label's brackets are backslash-escaped; the URL's parens/spaces are
    percent-encoded (a browser-equivalent, link-safe form). (Review S10.)
    """
    label = (label or "").replace("[", "\\[").replace("]", "\\]")
    safe_url = (url or "").replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[{label}]({safe_url})"


def _demote_headings(text: str) -> str:
    """Demote ``## `` headings in embedded provider text to ``#### ``.

    Raw provider outputs sit under a ``### provider`` heading, and providers
    are now instructed to emit ``## Sources`` sections — left as-is those
    become spurious top-level report sections, which downstream ``##``
    chunkers (brainkit's report ingestion) turn into junk notes. Fence-aware:
    ``## `` inside code blocks is content.
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            line = f"##{line}"
        out.append(line)
    return "\n".join(out)


class Formatter:
    """
    Formats InsightBundle and ProviderResult objects for output.

    Supports markdown and JSON output formats with consistent source formatting
    across all provider types.
    """

    def format_markdown(
        self,
        bundle: InsightBundle,
        include_raw: bool = True,
        system_config: dict[str, Any] | None = None,
        digest_markdown: str | None = None,
    ) -> str:
        """
        Format the insight bundle as markdown.

        Args:
            bundle: The InsightBundle to format
            include_raw: Whether to include raw provider outputs
            system_config: Optional system config snapshot to display
        """
        lines = [
            "# Research Report",
            "",
            f"**Topic:** {bundle.topic}",
        ]

        # Add keywords if present.
        # User-supplied seed keywords always shown when present.
        # Synthesized keywords (grounded in real findings) shown separately
        # when synthesis ran (i.e. when no user seeds were given).
        if bundle.keywords:
            lines.append(f"**Keywords:** {', '.join(bundle.keywords)}")
        if bundle.synthesized_keywords:
            lines.append(
                "**Keywords (synthesized from findings):** "
                + ", ".join(bundle.synthesized_keywords)
            )
        if bundle.provider_keywords:
            lines.append("")
            lines.append("**Provider keyword contributions:**")
            for provider, kws in bundle.provider_keywords.items():
                preview = ", ".join(kws[:5])
                suffix = f", ... ({len(kws)} total)" if len(kws) > 5 else ""
                lines.append(f"- *{provider}*: {preview}{suffix}")

        lines.extend(
            [
                f"**Time Window:** Last {bundle.days} days",
                f"**Providers:** {', '.join(bundle.providers_queried)}",
            ]
        )

        # Add system config info if available
        if system_config:
            preset = system_config.get("preset_name", "default")
            fingerprint = system_config.get("fingerprint", "")
            lines.append(f"**Model Preset:** {preset} (config: `{fingerprint}`)")

        lines.append("")

        if bundle.professional_overview_markdown:
            lines.extend(
                [
                    "---",
                    "",
                    "## Professional Overview",
                    "",
                    bundle.professional_overview_markdown,
                    "",
                ]
            )

        # Digest summary (concise, scannable overview of all findings)
        if digest_markdown:
            lines.extend(
                [
                    "---",
                    "",
                    "## Digest",
                    "",
                    digest_markdown,
                    "",
                ]
            )

        lines.extend(
            [
                "---",
                "",
            ]
        )

        # Meta summary
        lines.extend(
            [
                "## Consolidated Analysis",
                "",
                bundle.meta_summary,
                "",
                "---",
                "",
            ]
        )

        # Site research results
        if bundle.site_research:
            lines.extend(self._format_site_research(bundle.site_research, bundle.days))

        # User-curated sources (URLs cited in final article + file references
        # used as context only). Section is omitted entirely when both lists
        # are empty.
        if bundle.user_url_sources or bundle.user_file_sources:
            lines.extend(
                self._format_user_sources(
                    bundle.user_url_sources,
                    bundle.user_file_sources,
                )
            )

        # Individual summaries
        if bundle.individual_summaries:
            lines.extend(
                [
                    "## Provider Summaries",
                    "",
                ]
            )
            for provider, summary in bundle.individual_summaries.items():
                lines.extend(
                    [
                        f"### {provider.title()}",
                        "",
                        summary,
                        "",
                    ]
                )
            lines.extend(["---", ""])

        # Analytics summary
        successful_results = [r for r in bundle.provider_results if r.is_success]
        total_sources = sum(self.count_sources(r) for r in successful_results)

        lines.extend(
            [
                "## Analytics",
                "",
            ]
        )

        lines.extend(
            [
                "| Provider | Status | Model | Sources |",
                "|----------|--------|-------|---------|",
            ]
        )

        for result in bundle.provider_results:
            status = "✓ Success" if result.is_success else "✗ Failed"
            source_count = self.count_sources(result) if result.is_success else "-"
            lines.append(
                f"| {result.provider.title()} | {status} | {result.model} | {source_count} |"
            )

        lines.extend(
            [
                "",
                f"**Total:** {len(successful_results)}/{len(bundle.provider_results)} providers succeeded, {total_sources} sources cited",
                "",
            ]
        )

        lines.extend(
            [
                "---",
                "",
            ]
        )

        # Raw outputs
        if include_raw:
            lines.extend(
                [
                    "## Raw Provider Outputs",
                    "",
                ]
            )
            for result in bundle.provider_results:
                status = "✓" if result.is_success else "✗"
                source_count = self.count_sources(result) if result.is_success else 0
                source_info = f" | {source_count} sources" if source_count > 0 else ""
                lines.extend(
                    [
                        f"### {status} {result.provider.title()} ({result.model}){source_info}",
                        "",
                    ]
                )
                if result.error:
                    lines.extend(
                        [
                            f"**Error:** {result.error}",
                            "",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            _demote_headings(result.raw_text),
                        ]
                    )

                    # Append structured citations
                    citations = self.format_sources(result)
                    if citations:
                        lines.extend(
                            [
                                "",
                                citations,
                            ]
                        )

                    lines.extend(
                        [
                            "",
                        ]
                    )
                lines.extend(["---", ""])

        return "\n".join(lines)

    def format_json(self, bundle: InsightBundle) -> str:
        """Format the insight bundle as JSON."""
        return json.dumps(bundle.to_dict(), indent=2, ensure_ascii=False)

    def count_sources(self, result: ProviderResult) -> int:
        """
        Count the number of sources/citations in a provider result.

        Uses the normalized sources list if available, falls back to meta for
        backward compatibility.
        """
        # Prefer the normalized sources list
        if result.sources:
            return len(result.sources)

        # Fall back to counting from meta for backward compatibility
        return self._count_sources_from_meta(result.meta)

    def _format_site_research(
        self,
        site_research: SiteResearchBundle,
        days: int,
    ) -> list[str]:
        """
        Format the site research section.

        Args:
            site_research: The site research bundle
            days: Lookback window in days

        Returns:
            List of markdown lines
        """
        lines = [
            "## Keyword-based Site Research",
            "",
        ]

        # Show config info
        sites = site_research.config.get("sites", [])
        if sites:
            lines.append(f"**Sites:** {', '.join(s.title() for s in sites)}")
        lines.append(f"**Lookback:** Last {days} days")
        lines.append("")

        # Show results by site
        has_items = False
        for site, items in site_research.items_by_site.items():
            if not items:
                continue

            has_items = True
            lines.append(f"### {site.title()}")
            lines.append("")

            for i, item in enumerate(items, 1):
                # Title with link (escaped: title/url come from scraped content)
                lines.append(f"{i}. **{_md_link(item.title, item.url)}**")

                # Metadata line
                meta_parts = []
                if item.published_at:
                    meta_parts.append(f"Published: {item.published_at[:10]}")
                if item.author_or_channel:
                    meta_parts.append(f"Author: {item.author_or_channel}")
                if item.popularity_display:
                    meta_parts.append(item.popularity_display)

                if meta_parts:
                    lines.append(f"   - {' | '.join(meta_parts)}")

                # TL;DR bullets from summary
                if item.summary and item.summary.tldr:
                    for bullet in item.summary.tldr[:3]:
                        lines.append(f"   - {bullet}")

                lines.append("")

        if not has_items:
            lines.append("*No site research results found.*")
            lines.append("")

        # Show errors if any
        if site_research.errors:
            lines.append("**Errors:**")
            for err in site_research.errors[:5]:
                lines.append(f"- {err}")
            lines.append("")

        # Add Exa URL appendix if Exa results are present
        exa_items = site_research.items_by_site.get("exa", [])
        if exa_items:
            lines.extend(self._format_exa_appendix(exa_items))

        lines.extend(["---", ""])
        return lines

    def _format_user_sources(
        self,
        url_sources: list[UserUrlSource],
        file_sources: list[UserFileSource],
    ) -> list[str]:
        """
        Format user-curated sources.

        URL entries appear in 'User-Curated Sources' (citable). File entries
        appear in 'User-Provided Documents' as plain references — they are
        used only as context for the final article and must NOT be cited.
        """
        lines: list[str] = ["## User-Curated Sources", ""]

        if url_sources:
            lines.append("*Provided by the user; cited in the final article.*")
            lines.append("")
            for s in url_sources:
                label = s.title or s.url
                line = f"- {_md_link(label, s.url)}"
                if s.note:
                    line += f" — {s.note}"
                lines.append(line)
            lines.append("")

        if file_sources:
            lines.append("### User-Provided Documents (context only, not citations)")
            lines.append("")
            lines.append(
                "*Used as additional context for the final article. The "
                "documents themselves are not cited; URLs/book titles/works "
                "found inside them may be.*"
            )
            lines.append("")
            for f in file_sources:
                label = f.title or f.filename
                line = f"- {label}"
                if f.title and f.title != f.filename:
                    line += f" (`{f.filename}`)"
                if f.note:
                    line += f" — {f.note}"
                lines.append(line)
            lines.append("")

        lines.extend(["---", ""])
        return lines

    def _format_exa_appendix(self, items: list) -> list[str]:
        """
        Format an appendix with all Exa URLs for easy reference.

        Args:
            items: List of SiteItem objects from Exa

        Returns:
            List of markdown lines
        """
        lines = [
            "### Exa Search Results - Full URL List",
            "",
            f"*{len(items)} sources found via Exa deep search*",
            "",
        ]

        for i, item in enumerate(items, 1):
            title = item.title or "Untitled"
            url = item.url
            author = item.author_or_channel
            date = item.published_at[:10] if item.published_at else None

            # Build compact reference line
            ref_parts = []
            if author:
                ref_parts.append(author)
            if date:
                ref_parts.append(date)

            ref_str = f" ({', '.join(ref_parts)})" if ref_parts else ""
            lines.append(f"{i}. {_md_link(title, url)}{ref_str}")

        lines.append("")
        return lines

    def _count_sources_from_meta(self, meta: dict[str, Any]) -> int:
        """Count sources from provider-specific metadata (backward compatibility)."""
        count = 0

        # Define all source key patterns and their counting logic
        simple_keys = [
            "citations",
            "social_citations",
            "web_citations",
            "search_results",
            "social_search_results",
            "web_search_results",
        ]

        grounding_keys = [
            "grounding_chunks",
            "social_grounding_chunks",
            "web_grounding_chunks",
        ]

        annotation_keys = [
            "annotations",
            "social_annotations",
            "web_annotations",
        ]

        # Count simple list sources
        for key in simple_keys:
            if key in meta and isinstance(meta[key], list):
                count += len(meta[key])

        # Count grounding chunks (only those with valid URIs)
        for key in grounding_keys:
            if key in meta and isinstance(meta[key], list):
                count += len(
                    [c for c in meta[key] if "web" in c and c["web"].get("uri")]
                )

        # Count annotations (extract URL citations)
        for key in annotation_keys:
            if key in meta and isinstance(meta[key], list):
                for ann in meta[key]:
                    if (
                        (hasattr(ann, "url_citation") and ann.url_citation)
                        or (hasattr(ann, "url") and ann.url)
                        or (
                            isinstance(ann, dict)
                            and ("url_citation" in ann or "url" in ann)
                        )
                    ):
                        count += 1

        return count

    def format_sources(self, result: ProviderResult) -> str:
        """
        Format sources from a provider result as markdown.

        Uses the normalized sources list if available, falls back to meta for
        backward compatibility.
        """
        # Prefer the normalized sources list
        if result.sources:
            return self._format_normalized_sources(result.sources)

        # Fall back to formatting from meta for backward compatibility
        return self._format_sources_from_meta(result.meta)

    def _format_source_line(self, i: int, source: Source) -> str:
        """Format a single source as a markdown line with optional snippet."""
        label = source.title or source.url
        line = (
            f"{i}. {_md_link(label, source.url)}"
            if source.title
            else f"{i}. {source.url}"
        )

        # Add date if available
        if source.date:
            line += f" ({source.date})"

        # Add snippet if available
        if source.snippet:
            snip = source.snippet.strip().replace("\n", " ")
            if len(snip) > 200:
                snip = snip[:197] + "..."
            line += f" — {snip}"

        return line

    def _format_normalized_sources(self, sources: list[Source]) -> str:
        """Format normalized Source objects as markdown."""
        if not sources:
            return ""

        # Group by source type
        from researchkit.providers.base import SourceType

        social_sources = [s for s in sources if s.source_type == SourceType.SOCIAL]
        web_sources = [s for s in sources if s.source_type == SourceType.WEB]
        unknown_sources = [s for s in sources if s.source_type == SourceType.UNKNOWN]

        lines = []

        if social_sources:
            lines.append("\n**Social Sources:**")
            for i, source in enumerate(social_sources, 1):
                lines.append(self._format_source_line(i, source))

        if web_sources:
            lines.append("\n**Web Sources:**")
            for i, source in enumerate(web_sources, 1):
                lines.append(self._format_source_line(i, source))

        if unknown_sources:
            lines.append("\n**Sources:**")
            for i, source in enumerate(unknown_sources, 1):
                lines.append(self._format_source_line(i, source))

        return "\n".join(lines)

    def _format_sources_from_meta(self, meta: dict[str, Any]) -> str:
        """Format sources from provider-specific metadata (backward compatibility)."""
        lines = []

        # Dispatch table for different source types
        source_handlers = [
            # (key patterns, label prefix, formatter)
            (
                [
                    ("citations", "Citations"),
                    ("social_citations", "Social Citations"),
                    ("web_citations", "Web Citations"),
                ],
                self._format_citation_list,
            ),
            (
                [
                    ("grounding_chunks", "Grounding Sources"),
                    ("social_grounding_chunks", "Social Grounding Sources"),
                    ("web_grounding_chunks", "Web Grounding Sources"),
                ],
                self._format_grounding_chunks,
            ),
            (
                [
                    ("annotations", "Citations"),
                    ("social_annotations", "Social Citations"),
                    ("web_annotations", "Web Citations"),
                ],
                self._format_annotations,
            ),
            (
                [
                    ("search_results", "Search Results"),
                    ("social_search_results", "Social Search Results"),
                    ("web_search_results", "Web Search Results"),
                ],
                self._format_search_results,
            ),
        ]

        for key_label_pairs, formatter in source_handlers:
            for key, label in key_label_pairs:
                if key in meta and isinstance(meta[key], list) and meta[key]:
                    formatted = formatter(meta[key], label)
                    if formatted:
                        lines.extend(formatted)

        return "\n".join(lines)

    def _format_citation_list(self, items: list, label: str) -> list[str]:
        """Format a list of citation items."""
        lines = [f"\n**{label}:**"]
        for i, item in enumerate(items, 1):
            if isinstance(item, str):
                lines.append(f"{i}. {item}")
            elif isinstance(item, dict):
                url = item.get("url", "")
                title = item.get("title")
                if title and url:
                    lines.append(f"{i}. {_md_link(title, url)}")
                elif url:
                    lines.append(f"{i}. {url}")
        return lines

    def _format_grounding_chunks(self, chunks: list, label: str) -> list[str]:
        """Format Gemini grounding chunks."""
        lines = [f"\n**{label}:**"]
        count = 0
        for chunk in chunks:
            if "web" in chunk and chunk["web"].get("uri"):
                count += 1
                title = chunk["web"].get("title", "Source")
                uri = chunk["web"]["uri"]
                lines.append(f"{count}. {_md_link(title, uri)}")
        return lines if count > 0 else []

    def _format_annotations(self, annotations: list, label: str) -> list[str]:
        """Format OpenAI/Perplexity annotations."""
        urls = []
        for ann in annotations:
            if hasattr(ann, "url_citation") and ann.url_citation:
                url = getattr(ann.url_citation, "url", None)
                title = getattr(ann.url_citation, "title", None)
                urls.append({"url": url, "title": title})
            elif hasattr(ann, "url") and ann.url:
                urls.append({"url": ann.url, "title": getattr(ann, "title", None)})
            elif hasattr(ann, "file_citation") and ann.file_citation:
                pass  # Skip file citations
            elif isinstance(ann, dict):
                if "url_citation" in ann:
                    urls.append(
                        {
                            "url": ann["url_citation"].get("url"),
                            "title": ann["url_citation"].get("title"),
                        }
                    )
                elif "url" in ann:
                    urls.append({"url": ann.get("url"), "title": ann.get("title")})

        if not urls:
            return []

        lines = [f"\n**{label}:**"]
        for i, item in enumerate(urls, 1):
            if item.get("title") and item.get("url"):
                lines.append(f"{i}. {_md_link(item['title'], item['url'])}")
            elif item.get("url"):
                lines.append(f"{i}. {item['url']}")
        return lines

    def _format_search_results(self, results: list, label: str) -> list[str]:
        """Format search results."""
        lines = [f"\n**{label}:**"]
        count = 0
        for result in results:
            if isinstance(result, dict):
                title = result.get("title", "Result")
                url = result.get("url")
            else:
                title = getattr(result, "title", "Result")
                url = getattr(result, "url", None)

            if url:
                count += 1
                lines.append(f"{count}. {_md_link(title, url)}")
        return lines if count > 0 else []


# Default formatter instance for convenience
default_formatter = Formatter()


def format_as_markdown(
    bundle: InsightBundle,
    include_raw: bool = True,
    system_config: dict[str, Any] | None = None,
    digest_markdown: str | None = None,
) -> str:
    """
    Format the insight bundle as markdown using the default formatter.

    Args:
        bundle: The InsightBundle to format
        include_raw: Whether to include raw provider outputs
        system_config: Optional system config snapshot to display
        digest_markdown: Optional digest summary to include after header
    """
    return default_formatter.format_markdown(
        bundle,
        include_raw,
        system_config=system_config,
        digest_markdown=digest_markdown,
    )


def format_as_json(bundle: InsightBundle) -> str:
    """Format the insight bundle as JSON using the default formatter."""
    return default_formatter.format_json(bundle)


def count_sources(result: ProviderResult) -> int:
    """Count sources using the default formatter."""
    return default_formatter.count_sources(result)


def format_sources(result: ProviderResult) -> str:
    """Format sources using the default formatter."""
    return default_formatter.format_sources(result)
