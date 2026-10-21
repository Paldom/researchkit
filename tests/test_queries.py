"""Deterministic keyless query synthesis (PLAN 1.11)."""

from __future__ import annotations

import pytest

from researchkit.queries import arxiv_query, key_terms, keyword_query


def test_names_outrank_plain_words() -> None:
    terms = key_terms("Best practices to design and build an MCP server", max_terms=3)
    assert terms[0] == "MCP"


def test_dates_numbers_and_sentence_capitals_are_dropped() -> None:
    terms = key_terms(
        "Investigate posts published from 2026-07-23 through 2026-08-22 comparing "
        "Camoufox and Patchright for 10 sites",
        max_terms=4,
    )
    assert "Investigate" not in terms
    assert not any(t[0].isdigit() for t in terms)
    assert terms[:2] == ["Camoufox", "Patchright"]


def test_context_preamble_and_decision_tail_are_ignored() -> None:
    prompt = (
        "Context: researchkit is an open-source Python CLI that runs research.\n\n"
        "Research question: How does OpenAlex keyword search compare with "
        "Semantic Scholar?\n\nThe answer must support this decision: pick one API"
    )
    terms = key_terms(prompt, max_terms=4)
    assert "researchkit" not in terms and "API" not in terms
    assert {"OpenAlex", "Semantic", "Scholar"} <= set(terms)


def test_slash_pairs_split_but_versions_stay() -> None:
    assert {"YouTube", "Google"} <= set(key_terms("YouTube/Google transcript policy"))
    assert "HTTP/2" in key_terms("HTTP/2 multiplexing", max_terms=2)


def test_arxiv_is_an_and_of_terms_not_a_phrase() -> None:
    q = arxiv_query("How do retrieval-augmented writers cite sources?")
    assert '"' not in q and " AND " in q and q.startswith("all:")


def test_user_keywords_win() -> None:
    assert (
        keyword_query("long topic about many things", ["MinHash", "dedup"])
        == "MinHash dedup"
    )


@pytest.mark.parametrize(
    "topic",
    [
        "What is an FDE? How to be a good FDE?",
        "glob* patterns (and) [brackets] {braces}: why?",
        '"quoted phrase" vs ?? and **stars**',
        "",
        "???",
        "a" * 400,
    ],
)
def test_queries_never_carry_wildcards(topic: str) -> None:
    # OpenAlex returned HTTP 400 on '?' (it reads '?' and '*' as wildcards).
    q = keyword_query(topic)
    assert "?" not in q and "*" not in q and '"' not in q
    assert len(q.split()) <= 3
