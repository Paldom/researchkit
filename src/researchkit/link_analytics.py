"""
Link Analytics module for analyzing citation links.

Provides URL normalization, duplicate detection, domain extraction,
and analytics aggregation for links from result.json.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

NormalizationMode = Literal["strict", "loose"]
DatasetName = Literal["citations", "site_research"]

# Tracking query parameters to remove in loose mode
_TRACKING_KEYS_EXACT = {
    "gclid",
    "fbclid",
    "msclkid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "yclid",
    "ref",
    "source",
    "campaign",
}
_TRACKING_PREFIXES = ("utm_",)


@dataclass(frozen=True)
class LinkOccurrence:
    """A single occurrence of a link in the dataset."""

    dataset: DatasetName
    provider: str
    source_type: str
    url: str
    title: str | None = None


@dataclass(frozen=True)
class DuplicateGroup:
    """A group of duplicate URLs after normalization."""

    normalized_url: str
    canonical_url: str
    occurrences: int
    providers: tuple[str, ...]
    variants: tuple[str, ...]


@dataclass
class LinkAnalytics:
    """Complete analytics for a set of link occurrences."""

    dataset: str
    mode: NormalizationMode
    total_occurrences: int
    unique_urls: int
    duplicate_occurrences: int
    duplicate_rate: float
    unique_domains: int
    counts_by_provider: dict[str, int]
    counts_by_source_type: dict[str, int]
    top_domains: list[tuple[str, int]]
    top_duplicates: list[DuplicateGroup]
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return self.raw


def _safe_lower(s: str) -> str:
    """Safely lowercase a string."""
    return s.lower() if isinstance(s, str) else s


def normalize_url(url: str, mode: NormalizationMode = "loose") -> str:
    """
    Normalize URLs for comparison/dedup analytics.

    Args:
        url: The URL to normalize
        mode: Normalization mode
            - "strict": lowercase scheme+host, drop fragment, keep query intact
            - "loose": strict + drop tracking params, sort query params,
                      normalize empty path to "/" for http(s)

    Returns:
        Normalized URL string
    """
    url = (url or "").strip()
    if not url:
        return ""

    # urlsplit raises ValueError on malformed input (e.g. an unclosed IPv6
    # bracket "http://[fe80::1"); one bad URL in result.json must not crash the
    # whole link-analytics feature. (Review S9.)
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""

    scheme = _safe_lower(parts.scheme)
    netloc = _safe_lower(parts.netloc)
    path = parts.path or ""
    query = parts.query or ""
    fragment = ""  # always drop for comparison

    # Handle URLs missing scheme but having netloc-like content
    if not scheme and not netloc and url.startswith("www."):
        try:
            parts = urlsplit("https://" + url)
        except ValueError:
            return ""
        scheme = "https"
        netloc = _safe_lower(parts.netloc)
        path = parts.path or ""
        query = parts.query or ""

    # Normalize empty path for http(s) in loose mode
    if mode == "loose" and scheme in ("http", "https") and path == "":
        path = "/"

    # Remove tracking parameters in loose mode
    if mode == "loose" and query:
        q = []
        for k, v in parse_qsl(query, keep_blank_values=True):
            kl = k.lower()
            if kl in _TRACKING_KEYS_EXACT or any(
                kl.startswith(pfx) for pfx in _TRACKING_PREFIXES
            ):
                continue
            q.append((k, v))
        # Stable ordering reduces duplicates from reordered params
        q.sort(key=lambda kv: (kv[0], kv[1]))
        query = urlencode(q, doseq=True)

    return urlunsplit((scheme, netloc, path, query, fragment))


def extract_domain(url: str) -> str:
    """
    Extract the registered domain from a URL.

    Uses tldextract if available for accurate eTLD+1 extraction,
    otherwise falls back to netloc.

    Args:
        url: The URL to extract domain from

    Returns:
        Registered domain (e.g., "bbc.co.uk" from "forums.bbc.co.uk")
    """
    if not url:
        return ""
    try:
        import tldextract

        ext = tldextract.extract(url)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        return ext.domain or ext.suffix or ""
    except ImportError:
        # Fallback to netloc if tldextract not installed
        return _safe_lower(urlsplit(url).netloc)
    except Exception:
        return _safe_lower(urlsplit(url).netloc)


def occurrences_from_result_json(result_json: dict[str, Any]) -> list[LinkOccurrence]:
    """
    Extract citation links from result.json as LinkOccurrence objects.

    Args:
        result_json: Parsed result.json data with provider_results[].sources[]

    Returns:
        List of LinkOccurrence objects
    """
    occ: list[LinkOccurrence] = []
    for pr in result_json.get("provider_results", []):
        provider = pr.get("provider", "unknown")
        for s in pr.get("sources", []) or []:
            url = s.get("url", "")
            if url:
                occ.append(
                    LinkOccurrence(
                        dataset="citations",
                        provider=provider,
                        source_type=s.get("source_type", "unknown"),
                        url=url,
                        title=s.get("title"),
                    )
                )
    return occ


def occurrences_from_site_research(result_json: dict[str, Any]) -> list[LinkOccurrence]:
    """
    Extract URLs from site_research.items_by_site as LinkOccurrence objects.

    Args:
        result_json: Parsed result.json data with site_research.items_by_site

    Returns:
        List of LinkOccurrence objects from site research (Exa, etc.)
    """
    occ: list[LinkOccurrence] = []
    site_research = result_json.get("site_research", {})
    if not site_research:
        return occ

    items_by_site = site_research.get("items_by_site", {})
    for site, items in items_by_site.items():
        for item in items or []:
            url = item.get("url", "")
            if url:
                occ.append(
                    LinkOccurrence(
                        dataset="site_research",
                        provider=site,  # medium, youtube, exa
                        source_type="site_research",
                        url=url,
                        title=item.get("title"),
                    )
                )
    return occ


def analyze_occurrences(
    occurrences: Iterable[LinkOccurrence],
    *,
    dataset_label: str,
    mode: NormalizationMode = "loose",
    top_n_domains: int = 20,
    top_n_duplicates: int = 20,
    keep_variants_per_group: int = 5,
) -> LinkAnalytics:
    """
    Analyze a collection of link occurrences.

    Args:
        occurrences: Iterable of LinkOccurrence objects
        dataset_label: Label for this dataset (e.g., "citations")
        mode: URL normalization mode ("strict" or "loose")
        top_n_domains: Number of top domains to include
        top_n_duplicates: Number of top duplicate groups to include
        keep_variants_per_group: Max variants to store per duplicate group

    Returns:
        LinkAnalytics object with computed metrics
    """
    occ_list = [o for o in occurrences if o.url]
    total = len(occ_list)

    norm_to_items: dict[str, list[LinkOccurrence]] = defaultdict(list)
    norm_to_variants: dict[str, set[str]] = defaultdict(set)

    provider_counter: Counter[str] = Counter()
    source_type_counter: Counter[str] = Counter()

    for o in occ_list:
        provider_counter[o.provider] += 1
        source_type_counter[o.source_type] += 1

        norm = normalize_url(o.url, mode=mode)
        if norm:
            norm_to_items[norm].append(o)
            norm_to_variants[norm].add(o.url)

    unique_urls = len(norm_to_items)
    # Count duplicates from the grouped occurrences directly (sum of extras per
    # normalized URL). Using `total - unique_urls` inflated the count by every
    # occurrence whose URL normalizes to "" (counted in total, excluded from
    # norm_to_items). (Review S9.)
    counted = sum(len(v) for v in norm_to_items.values())
    duplicate_occurrences = counted - unique_urls if counted >= unique_urls else 0
    duplicate_rate = (duplicate_occurrences / counted) if counted else 0.0

    # Domain counts from normalized URLs
    domain_counter: Counter[str] = Counter()
    for norm_url, items in norm_to_items.items():
        domain = extract_domain(norm_url)
        domain_counter[domain] += len(items)

    # Build duplicate groups (URLs with more than one occurrence)
    groups: list[DuplicateGroup] = []
    for norm_url, items in norm_to_items.items():
        if len(items) <= 1:
            continue
        providers = sorted({i.provider for i in items})
        variants = sorted(norm_to_variants[norm_url])[:keep_variants_per_group]
        # Choose canonical = shortest variant or normalized itself
        canonical = min(variants, key=len) if variants else norm_url
        groups.append(
            DuplicateGroup(
                normalized_url=norm_url,
                canonical_url=canonical,
                occurrences=len(items),
                providers=tuple(providers),
                variants=tuple(variants),
            )
        )
    groups.sort(key=lambda g: g.occurrences, reverse=True)

    # Build raw dict for JSON serialization
    raw = {
        "dataset": dataset_label,
        "mode": mode,
        "summary": {
            "total_occurrences": total,
            "unique_urls": unique_urls,
            "duplicate_occurrences": duplicate_occurrences,
            "duplicate_rate": duplicate_rate,
            "unique_domains": len([d for d in domain_counter if d]),
        },
        "counts_by_provider": dict(provider_counter),
        "counts_by_source_type": dict(source_type_counter),
        "top_domains": domain_counter.most_common(top_n_domains),
        "top_duplicates": [
            {
                "normalized_url": g.normalized_url,
                "canonical_url": g.canonical_url,
                "occurrences": g.occurrences,
                "providers": list(g.providers),
                "variants": list(g.variants),
            }
            for g in groups[:top_n_duplicates]
        ],
    }

    return LinkAnalytics(
        dataset=dataset_label,
        mode=mode,
        total_occurrences=total,
        unique_urls=unique_urls,
        duplicate_occurrences=duplicate_occurrences,
        duplicate_rate=duplicate_rate,
        unique_domains=raw["summary"]["unique_domains"],
        counts_by_provider=dict(provider_counter),
        counts_by_source_type=dict(source_type_counter),
        top_domains=domain_counter.most_common(top_n_domains),
        top_duplicates=groups[:top_n_duplicates],
        raw=raw,
    )


def analyze_project_links(
    result_json: dict[str, Any],
    *,
    mode: NormalizationMode = "loose",
    top_n_domains: int = 20,
    top_n_duplicates: int = 20,
) -> dict[str, Any]:
    """
    Analyze links from result.json.

    Convenience function that handles loading the dataset.

    Args:
        result_json: Parsed result.json data
        mode: URL normalization mode
        top_n_domains: Number of top domains to include
        top_n_duplicates: Number of top duplicate groups to include

    Returns:
        Dict with "citations" and "site_research" analytics
    """
    outputs: dict[str, Any] = {}

    # Citations from AI providers (openai, gemini, grok, perplexity)
    occ = occurrences_from_result_json(result_json)
    if occ:
        outputs["citations"] = analyze_occurrences(
            occ,
            dataset_label="citations",
            mode=mode,
            top_n_domains=top_n_domains,
            top_n_duplicates=top_n_duplicates,
        ).to_dict()

    # Site research links (medium, youtube, exa)
    site_occ = occurrences_from_site_research(result_json)
    if site_occ:
        outputs["site_research"] = analyze_occurrences(
            site_occ,
            dataset_label="site_research",
            mode=mode,
            top_n_domains=top_n_domains,
            top_n_duplicates=top_n_duplicates,
        ).to_dict()

    return outputs
