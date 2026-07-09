"""Shared utility functions."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


def slugify(text: str, max_length: int = 30) -> str:
    """
    Convert text to a URL/filename-friendly slug.

    Accented Latin is transliterated to ASCII (``café`` -> ``cafe``). A topic in a
    non-Latin script (CJK, Cyrillic, …) that would otherwise collapse to an empty
    slug — and collide with every other such topic under the shared ``untitled``
    fallback — instead gets a short stable hash of the original so distinct
    topics get distinct folder names. (Review utils.py slugify.)

    Args:
        text: The text to slugify
        max_length: Maximum length of the slug (default: 30)

    Returns:
        A lowercase, hyphen-separated slug
    """
    # NFKD + ASCII-drop transliterates accents; non-Latin scripts fall away.
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    slug = ascii_text.lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rsplit("-", 1)[0]
    if slug:
        return slug
    if text.strip():
        return f"topic-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"
    return "untitled"


def _repair_truncated_json(text: str) -> dict[str, Any] | None:
    """Best-effort parse of a truncated JSON object.

    Models sometimes stop mid-array (``{ "keywords": [ "a", "b``). Walk the
    text tracking string/bracket state, cut back to the last complete value,
    and close whatever brackets remain open.
    """
    start = text.find("{")
    if start < 0:
        return None
    s = text[start:]
    stack: list[str] = []
    in_str = False
    escaped = False
    # (index-past-value, open-bracket stack) after each complete string/bracket;
    # tried newest-first because the newest cut may end on a dangling key.
    snapshots: list[tuple[int, list[str]]] = []
    for i, ch in enumerate(s):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
                snapshots.append((i + 1, list(stack)))
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None  # malformed, not merely truncated
            stack.pop()
            snapshots.append((i + 1, list(stack)))
    for cut, open_stack in reversed(snapshots[-20:]):  # bounded backtrack
        # cuts land right after a closing quote/bracket, so no comma trimming
        candidate = s[:cut] + "".join(reversed(open_stack))
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a model response.

    Tolerates ``` fences, prose preamble/trailer, and (best-effort) truncated
    output. Returns None when no object can be recovered.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate).strip()
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Truncation repair FIRST: a truncated outer object often contains a
    # complete nested {...}, and the inner-object scan below would wrongly
    # return that fragment when the full object is recoverable. The repair's
    # backtracking also finds a complete first object surrounded by prose.
    repaired = _repair_truncated_json(candidate)
    if repaired is not None:
        return repaired
    # Fallback scan for the first COMPLETE object: raw_decode stops at its
    # end, so unbalanced prose/thinking around the JSON is tolerated (a
    # greedy {.*} regex would wrongly span multiple objects).
    decoder = json.JSONDecoder()
    for start, ch in enumerate(candidate):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
