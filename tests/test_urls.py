"""One canonical source identity (PLAN 2.1); brainkit keys notes on it."""

from __future__ import annotations

import pytest

from researchkit.urls import canonical_url, host_of, origin_key

PAIRS = [
    # (input, canonical) — identity must stay what earlier runs stored
    ("https://Example.com/Path?utm_source=x&a=1#frag", "https://example.com/Path?a=1"),
    ("HTTPS://EXAMPLE.COM/a", "https://example.com/a"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("https://example.com:8443/a", "https://example.com:8443/a"),
    ("https://www.example.com/a/", "https://www.example.com/a/"),  # kept: identity
    ("https://x.com/a?fbclid=1&gclid=2", "https://x.com/a"),
    ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),  # order kept
    ("  https://example.com/a  ", "https://example.com/a"),
    ("http://[fe80::1", "http://[fe80::1"),  # malformed: returned, never raises
]


@pytest.mark.parametrize(("url", "expected"), PAIRS)
def test_canonical_url(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


def test_materials_uses_the_same_rule() -> None:
    from researchkit.materials import _clean_citation_url, _normalize_url

    for url, expected in PAIRS[:-1]:
        assert _clean_citation_url(url) == expected == _normalize_url(url)


def test_origin_key_merges_cosmetic_variants_only() -> None:
    assert origin_key("https://www.example.com/a/") == origin_key(
        "https://example.com/a"
    )
    assert origin_key("https://example.com/a") != origin_key("https://example.com/b")
    assert host_of("https://WWW.Example.com/x") == "example.com"
