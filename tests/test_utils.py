"""Tests for shared utilities — tolerant JSON extraction (FB #5)."""

from __future__ import annotations

from researchkit.utils import extract_json_object


class TestExtractJsonObject:
    def test_plain_object(self) -> None:
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced(self) -> None:
        text = '```json\n{"keywords": ["a b"]}\n```'
        assert extract_json_object(text) == {"keywords": ["a b"]}

    def test_prose_preamble_and_trailer(self) -> None:
        text = 'Sure! Here you go:\n{"a": [1, 2]}\nHope that helps.'
        assert extract_json_object(text) == {"a": [1, 2]}

    def test_truncated_array_is_repaired(self) -> None:
        # The exact failure observed in the field: model output cut mid-array.
        text = '{ "keywords": [ "agent memory", "self improving agents", "orch'
        assert extract_json_object(text) == {
            "keywords": ["agent memory", "self improving agents"]
        }

    def test_truncated_nested_object_is_repaired(self) -> None:
        text = '{"improved_topic": "x", "meta": {"decompose": true, "subqueries": ["a"'
        data = extract_json_object(text)
        assert data is not None
        assert data["improved_topic"] == "x"
        assert data["meta"]["subqueries"] == ["a"]

    def test_truncation_with_complete_nested_object_returns_full_object(self) -> None:
        # Red-team F1: the inner-object scan must not win over the repair —
        # a complete nested {...} inside a truncated outer object used to be
        # returned as the whole result (losing improved_topic/keywords).
        text = '{"improved_topic": "x", "meta": {"decompose": true}'
        assert extract_json_object(text) == {
            "improved_topic": "x",
            "meta": {"decompose": True},
        }
        text = '{"subs": [{"t": "a"}, {"t": "b'
        assert extract_json_object(text) == {"subs": [{"t": "a"}]}
        text = '{"note": "empty {} here", "keywords": ["agent memory", "self improving'
        assert extract_json_object(text) == {
            "note": "empty {} here",
            "keywords": ["agent memory"],
        }

    def test_truncated_fenced(self) -> None:
        text = '```json\n{ "keywords": [ "a b", "c d'
        assert extract_json_object(text) == {"keywords": ["a b"]}

    def test_first_complete_object_wins_over_repair(self) -> None:
        text = 'thinking... {"pick": "me"} and then {"not": "me"}'
        assert extract_json_object(text) == {"pick": "me"}

    def test_hopeless_input_returns_none(self) -> None:
        assert extract_json_object("") is None
        assert extract_json_object("no json here") is None
        assert extract_json_object("{") is None
        assert extract_json_object("]}malformed{[") is None

    def test_escaped_quotes_and_braces_in_strings(self) -> None:
        text = '{"a": "brace } and \\" quote", "b": [1'
        data = extract_json_object(text)
        assert data is not None
        assert data["a"] == 'brace } and " quote'
