"""Deterministic query synthesis for keyword/lexical search APIs.

The keyless providers (Hacker News, OpenAlex, arXiv, Stack Exchange) used to
send the raw research topic: a sentence-long question. Hacker News found
sources in 1% of 248 past runs, Stack Exchange 0%, arXiv 3% (exact-phrase
match), OpenAlex 24% (and HTTP 400 on any ``?``, which it reads as a
wildcard). These APIs AND their terms or match phrases, so a paragraph is a
query nothing satisfies.

This module turns a topic into a few ranked key terms, deterministically and
with no model call. Research (topic 14) found valid syntax beat LLM
rewriting and that HyDE/Query2doc add unsupported terms. Each provider then
formats them for its API.
"""

from __future__ import annotations

import re

# Function words plus research-question filler that never helps a lexical
# index ("how do", "latest", "compare", "late 2026").
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "either",
        "else",
        "ever",
        "every",
        "few",
        "for",
        "from",
        "further",
        "get",
        "gets",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "let",
        "me",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "per",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "upon",
        "us",
        "very",
        "via",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "you",
        "your",
        "yours",
        "across",
        "actually",
        "along",
        "answer",
        "answers",
        "approach",
        "approaches",
        "around",
        "based",
        "best",
        "better",
        "case",
        "cases",
        "choose",
        "compare",
        "compared",
        "comparison",
        "current",
        "currently",
        "decide",
        "decision",
        "deep",
        "describe",
        "detail",
        "detailed",
        "different",
        "does",
        "doing",
        "done",
        "early",
        "effect",
        "effective",
        "effects",
        "else",
        "end",
        "ensure",
        "especially",
        "etc",
        "evaluate",
        "evidence",
        "example",
        "examples",
        "exist",
        "existing",
        "explain",
        "fact",
        "factors",
        "find",
        "findings",
        "focus",
        "following",
        "found",
        "give",
        "given",
        "good",
        "guide",
        "happen",
        "happens",
        "help",
        "how-to",
        "important",
        "improve",
        "include",
        "including",
        "instead",
        "issue",
        "issues",
        "key",
        "kind",
        "know",
        "known",
        "late",
        "latest",
        "lead",
        "learn",
        "least",
        "like",
        "likely",
        "list",
        "look",
        "made",
        "main",
        "make",
        "makes",
        "making",
        "many",
        "matter",
        "may",
        "mean",
        "means",
        "measure",
        "might",
        "mostly",
        "much",
        "must",
        "need",
        "needs",
        "new",
        "next",
        "often",
        "one",
        "ones",
        "overall",
        "part",
        "people",
        "point",
        "possible",
        "practice",
        "practical",
        "pros",
        "cons",
        "provide",
        "question",
        "questions",
        "rather",
        "really",
        "reason",
        "recent",
        "recently",
        "related",
        "research",
        "researchers",
        "result",
        "results",
        "right",
        "say",
        "says",
        "see",
        "seem",
        "show",
        "shows",
        "significant",
        "since",
        "state",
        "still",
        "study",
        "support",
        "take",
        "tell",
        "thing",
        "things",
        "think",
        "today",
        "top",
        "trend",
        "trends",
        "try",
        "type",
        "types",
        "understand",
        "use",
        "used",
        "useful",
        "uses",
        "using",
        "vs",
        "versus",
        "want",
        "way",
        "ways",
        "well",
        "whether",
        "work",
        "works",
        "worth",
        "yet",
        "anecdotes",
        "claims",
        "community",
        "criticism",
        "debate",
        "discussion",
        "discussions",
        "disputes",
        "investigate",
        "limited",
        "official",
        "platform",
        "platforms",
        "posts",
        "practitioner",
        "practitioners",
        "published",
        "publish",
        "social",
        "typical",
    ]
)
# Context preambles the CLI prepends ("Context: researchkit is …") and
# instruction tails ("The answer must support this decision: …") are not the
# topic: drop them before extracting terms.
_PREAMBLE = re.compile(r"^\s*context:.*?(?:research question:|$)", re.I | re.S)
_TAIL = re.compile(
    r"(the answer must support this decision:|respond only).*$", re.I | re.S
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#._/-]*[A-Za-z0-9+#]|[A-Za-z0-9]")
_YEAR = re.compile(r"^(19|20)\d\d$")
# Dates and bare numbers are window filters or noise, never a search term.
_DATE_OR_NUMBER = re.compile(r"^(\d+([.,]\d+)?|\d{4}-\d{2}(-\d{2})?)$")
_SENTENCE_END = re.compile(r"[.?!:;]\s*$")


def _technical(token: str, *, sentence_start: bool) -> bool:
    """Names and identifiers rank first: Exa, gpt-6.1, OpenAlex, HTTP/2, C++.

    A capital that only marks the start of a sentence is not a name, and a
    plain lowercase hyphenation (``discussion-platform``) is just a word.
    """
    has_digit = any(c.isdigit() for c in token)
    inner_caps = any(c.isupper() for c in token[1:])
    if has_digit or inner_caps or any(c in token for c in "+#._/"):
        return True
    if token.isupper() and len(token) >= 2:
        return True
    return token[0].isupper() and not sentence_start


def key_terms(topic: str, *, max_terms: int = 4) -> list[str]:
    """Up to ``max_terms`` distinct key terms from ``topic``, best first.

    Technical tokens (names, versions, identifiers) outrank plain words. Ties
    keep their order of appearance, so the result is stable for a topic.
    Years are dropped (the time window is a separate API filter).
    """
    text = _TAIL.sub("", _PREAMBLE.sub("", topic))
    seen: set[str] = set()
    ranked: list[tuple[int, int, str]] = []
    # "X/Twitter", "YouTube/Google" are two terms; "HTTP/2" stays one.
    text = re.sub(r"(?<=[A-Za-z]{2})/(?=[A-Za-z]{2})", " ", text)
    for position, match in enumerate(_TOKEN.finditer(text)):
        token = match.group().strip("._/-")
        low = token.lower()
        before = text[: match.start()].rstrip()
        start = not before or bool(_SENTENCE_END.search(before))
        if (
            not token
            or low in _STOPWORDS
            or low in seen
            or _YEAR.match(token)
            or _DATE_OR_NUMBER.match(token)
            or (len(token) < 3 and not token.isupper())
        ):
            continue
        seen.add(low)
        ranked.append(
            (0 if _technical(token, sentence_start=start) else 1, position, token)
        )
    ranked.sort()
    return [t for _, _, t in ranked[:max_terms]]


def _clean(term: str) -> str:
    """Strip characters the APIs treat as syntax (OpenAlex wildcards etc.)."""
    return re.sub(r'[?*"()\[\]{}:]', "", term)


def keyword_query(
    topic: str, keywords: tuple[str, ...] | list[str] = (), *, max_terms: int = 3
) -> str:
    """A short space-separated query: user keywords first, else key terms."""
    terms = [_clean(k) for k in keywords if _clean(k).strip()][:max_terms]
    if not terms:
        terms = [_clean(t) for t in key_terms(topic, max_terms=max_terms)]
    return " ".join(t for t in terms if t)


def arxiv_query(topic: str, keywords: tuple[str, ...] | list[str] = ()) -> str:
    """arXiv ``search_query``: AND of ``all:`` terms (never one exact phrase)."""
    terms = keyword_query(topic, keywords, max_terms=3).split()
    return " AND ".join(f"all:{t}" for t in terms) if terms else ""
