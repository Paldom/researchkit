"""Tests for the LLM council's pure parsing/merge logic (review L10, L8)."""

from __future__ import annotations

from researchkit.council import (
    CouncilProposal,
    LLMCouncil,
    _clean_keywords,
    _clean_subqueries,
    _coerce_decompose,
    _extract_json,
)


class TestCoerceDecompose:
    def test_real_booleans(self) -> None:
        assert _coerce_decompose(True) is True
        assert _coerce_decompose(False) is False

    def test_string_false_is_false(self) -> None:
        # bool("false") is True in Python — the bug this guards. (Review L8.)
        assert _coerce_decompose("false") is False
        assert _coerce_decompose("False") is False

    def test_string_true(self) -> None:
        assert _coerce_decompose("true") is True
        assert _coerce_decompose("TRUE") is True

    def test_other_types(self) -> None:
        assert _coerce_decompose(None) is False
        assert _coerce_decompose(1) is False


class TestExtractJson:
    def test_plain_object(self) -> None:
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_fenced(self) -> None:
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_nested_object_with_prose(self) -> None:
        text = 'Here you go:\n{"improved_topic": "x", "meta": {"k": "v"}} — done'
        assert _extract_json(text) == {"improved_topic": "x", "meta": {"k": "v"}}

    def test_unparseable(self) -> None:
        assert _extract_json("no json here") is None


class TestCleanKeywords:
    def test_dedup_and_min_words_and_cap(self) -> None:
        raw = ["ai agents", "AI Agents", "x", "chatbot deflection rates", "one two"]
        out = _clean_keywords(raw, count=2)
        assert out == ["ai agents", "chatbot deflection rates"]

    def test_non_list(self) -> None:
        assert _clean_keywords("nope", 5) == []


class TestCleanSubqueries:
    def test_dedup_and_cap(self) -> None:
        assert _clean_subqueries(["a", "A", "b", "c"], limit=2) == ["a", "b"]


class TestMergeWithoutBoss:
    def _council(self) -> LLMCouncil:
        return LLMCouncil(members=["m1", "m2"], boss="b", max_subprojects=5)

    def test_majority_decompose_and_keyword_union(self) -> None:
        council = self._council()
        valid = [
            CouncilProposal(
                member="m1",
                lens="l",
                improved_topic="Topic one",
                keywords=["ai agents", "agent frameworks"],
                decompose=True,
                subqueries=["sub a", "sub b"],
            ),
            CouncilProposal(
                member="m2",
                lens="l",
                improved_topic="A longer refined topic here",
                keywords=["agent frameworks", "llm tooling"],
                decompose=True,
                subqueries=["sub c", "sub d"],
            ),
        ]
        result = council._merge_without_boss("raw", valid, count=10)
        assert result.decompose is True
        assert len(result.subqueries) >= 2
        # keyword union, deduped, order-preserved
        assert result.keywords == ["ai agents", "agent frameworks", "llm tooling"]
        assert result.boss_synthesized is False

    def test_no_majority_decompose(self) -> None:
        council = self._council()
        valid = [
            CouncilProposal(
                member="m1", lens="l", improved_topic="t1", decompose=False
            ),
            CouncilProposal(
                member="m2",
                lens="l",
                improved_topic="t2",
                decompose=True,
                subqueries=["a", "b"],
            ),
        ]
        # 1 of 2 votes -> not a majority -> no decompose
        result = council._merge_without_boss("raw", valid, count=10)
        assert result.decompose is False
        assert result.subqueries == []
