"""Tests for researchkit.slugify_topic."""

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from researchkit import __version__, slugify_topic


def test_version_metadata_resolves() -> None:
    # No hardcoded number: the pin would break every release's publish gate.
    assert __version__


def test_basic_normalization() -> None:
    assert slugify_topic("  LLM Council: Research!  ") == "llm-council-research"


def test_collapses_separator_runs() -> None:
    assert slugify_topic("a --- b") == "a-b"


def test_empty_topic_rejected() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        slugify_topic("!!! ---")


@given(st.text())
def test_idempotent_or_rejected(topic: str) -> None:
    try:
        slug = slugify_topic(topic)
    except ValueError:
        return
    assert slugify_topic(slug) == slug


@given(st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1))
def test_ascii_alnum_input_yields_slug(topic: str) -> None:
    slug = slugify_topic(topic)
    assert slug
    assert all(c.isascii() and (c.isalnum() or c == "-") for c in slug)
