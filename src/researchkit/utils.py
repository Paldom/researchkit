"""Shared utility functions."""

from __future__ import annotations

import hashlib
import re
import unicodedata


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
