"""One source identity for every cited URL (PLAN 2.1).

``canonical_url`` is the identity a source carries everywhere: materials
frontmatter, the citation table, claims, and brainkit's source notes (which
key on it). It is deliberately conservative and backward compatible: tracking
params, the fragment and default ports go, and scheme and host are
lowercased, but ``www.``, trailing slashes and parameter order are kept. Two
distinct pages must never merge, and existing brains must keep their keys.

``origin_key`` is looser and is used only to count *independent origins*
(corroboration). There, ``www.`` and a trailing slash are noise, and merging
two near-identical addresses errs toward under-counting corroboration, which
is the safe direction.
"""

from __future__ import annotations

from urllib.parse import urlsplit

TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "ref_src", "share_id")
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def strip_tracking(query: str) -> str:
    """Drop analytics params (utm_source=openai etc.) that defeat dedup."""
    kept = [
        pair
        for pair in query.split("&")
        if pair
        and not pair.split("=", 1)[0].lower().startswith(TRACKING_PARAM_PREFIXES)
    ]
    return "&".join(kept)


def canonical_url(url: str) -> str:
    """The source identity: tracking params, fragment and default port dropped.

    Case is preserved in path and query (paths can be case-sensitive).
    Malformed input comes back stripped rather than raising.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    host, sep, port = netloc.rpartition(":")
    if sep and port == _DEFAULT_PORTS.get(scheme) and "]" not in port:
        netloc = host
    query = strip_tracking(parts.query)
    rebuilt = f"{scheme}://{netloc}{parts.path}"
    return f"{rebuilt}?{query}" if query else rebuilt


def origin_key(url: str) -> str:
    """Looser key for counting independent origins (never an identity)."""
    canon = canonical_url(url)
    try:
        parts = urlsplit(canon)
    except ValueError:
        return canon
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


def host_of(url: str) -> str:
    """Lowercased host without ``www.`` (for distinct-host checks)."""
    try:
        return urlsplit(url.strip()).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""
