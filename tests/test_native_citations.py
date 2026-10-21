"""PLAN A.2 / A.9: provider-native citations, recorded API shapes, offline."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from researchkit.citations import (
    gemini_native,
    marker_native,
    native_spans,
    openai_native,
)
from researchkit.providers.base import ProviderResult, SourceType

_TEXT = "Codex caches web search by default. Live mode costs 3x more (docs)."


def test_openai_responses_and_chat_annotation_shapes() -> None:
    at = _TEXT.index("(docs)")
    responses = NS(
        type="url_citation", url="https://a.io/live", start_index=at, end_index=at + 6
    )
    chat = {"url_citation": {"url": "https://a.io/cache", "start_index": 3}}
    assert openai_native(_TEXT, [responses, chat, NS(type="file_citation")]) == [
        {"url": "https://a.io/live", "text": "Live mode costs 3x more (docs)."},
        {"url": "https://a.io/cache", "text": "Codex caches web search by default."},
    ]


def test_gemini_supports_map_segments_to_resolved_chunks() -> None:
    supports = [
        {
            "segment": {"text": "Codex caches  web search."},
            "grounding_chunk_indices": [0, 2],
        },
        {"segment": {"text": ""}, "grounding_chunk_indices": [1]},
        {"segment": {"text": "Out of range."}, "grounding_chunk_indices": [9]},
    ]
    chunk_urls = ["https://a.io/x", "https://b.io/y", None]
    assert gemini_native(supports, chunk_urls) == [
        {"url": "https://a.io/x", "text": "Codex caches web search."}
    ]


def test_perplexity_markers_index_its_result_list() -> None:
    text = "Caching is on by default [1]. Live costs more [2][3]. Bad [7]."
    got = marker_native(text, ["https://a.io/1", "https://a.io/2", "https://a.io/3"])
    assert got == [
        {"url": "https://a.io/1", "text": "Caching is on by default ."},
        {"url": "https://a.io/2", "text": "Live costs more ."},
        {"url": "https://a.io/3", "text": "Live costs more ."},
    ]


def test_gemini_provider_captures_supports_from_a_recorded_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchkit.providers.gemini_provider import GeminiProvider

    response = NS(
        text="Codex caches web search.",
        candidates=[
            NS(
                grounding_metadata=NS(
                    grounding_chunks=[
                        NS(web=NS(uri="https://redirect/1", title="a.io")),
                        NS(retrieved_context=NS()),  # non-web chunk keeps its index
                        NS(web=NS(uri="https://redirect/2", title="b.io")),
                    ],
                    grounding_supports=[
                        NS(
                            segment=NS(text="Codex caches web search."),
                            grounding_chunk_indices=[2],
                        )
                    ],
                )
            )
        ],
    )
    p = GeminiProvider.__new__(GeminiProvider)
    p.model_name = "gemini-3.8-flash"
    p.provider_name = "gemini"
    client = NS(models=NS(generate_content=lambda **kw: response))
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    text, chunks, supports = p._run_query(client, "s", "u", (now, now))
    assert text == "Codex caches web search."
    assert [c["web"]["uri"] for c in chunks] == [
        "https://redirect/1",
        None,
        "https://redirect/2",
    ]
    monkeypatch.setattr(
        p, "_resolve_redirect_url", lambda u: (u.replace("redirect", "real"), None)
    )
    p._extract_sources(chunks, SourceType.WEB)
    urls = [(c.get("web") or {}).get("resolved") for c in chunks]
    assert gemini_native(supports, urls) == [
        {"url": "https://real/2", "text": "Codex caches web search."}
    ]


def test_claims_carry_native_spans(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen API-style run through the M2 pipeline offline (A.9)."""
    import researchkit.publish as pub
    from tests.test_publish import TestPublish, _project

    bundle = TestPublish()._bundle()
    bundle.provider_results.append(
        ProviderResult(
            "openai",
            "gpt-5.4-mini",
            _TEXT,
            meta={
                "native_citations": [
                    {
                        "url": "https://a.example/cache/",
                        "text": "Codex caches web search by default.",
                    }
                ]
            },
        )
    )
    assert native_spans(bundle.provider_results) == {
        "a.example/cache": ["Codex caches web search by default."]
    }

    def fake_write(self: Any, prompt: str) -> str:
        if self.stage_name == "report_body":
            return "## Codex caches by default\nCodex caches results [1]. Gemini offsets [2]."
        return "## TL;DR\nCodex caches [1]."

    monkeypatch.setattr(pub._Writer, "write", fake_write)
    result = pub.publish(
        topic="t",
        days=7,
        bundle=bundle,
        project_dir=_project(tmp_path),
        effective_models=None,
        run_id="r1",
    )
    by_url = {c["canonical_url"]: c["native_spans"] for c in result.claims}
    assert by_url["https://a.example/cache"] == ["Codex caches web search by default."]
    assert by_url["https://b.example/gemini"] == []
