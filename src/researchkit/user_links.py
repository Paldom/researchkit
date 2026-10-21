"""Fetch the text behind a user-curated link, including YouTube transcripts.

A URL added with ``add-source`` used to be metadata: the final article was told
the link existed and was free to cite it, but nothing ever read the page. A
YouTube link was therefore worth exactly its title. This module fetches the
content so the link actually contributes.

Two paths, in preference order:

1. ``summarize`` (https://github.com/steipete/summarize) when it is on PATH.
   ``summarize <url> --extract --plain`` needs no API key and covers web pages,
   PDFs, YouTube and podcasts (published transcripts first) behind one
   interface — which is why it is preferred over hand-rolling per-platform
   extractors here.
2. The SSRF-guarded fetcher already used by the materials archive. Fine for
   ordinary pages; useless for YouTube, whose page carries no transcript. A
   YouTube link with no ``summarize`` installed is reported as such rather
   than silently archived as a shell of navigation chrome.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from researchkit.providers.base import CLI_QUICK_TIMEOUT, cli_harness_timeout
from researchkit.safe_io import run_subprocess, safe_fetch_text

logger = logging.getLogger(__name__)

__all__ = ["LinkContent", "fetch_link", "is_youtube_url", "youtube_video_id"]

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)

# Max characters kept from one link; the same order of magnitude as the
# per-file budget for user documents.
_MAX_CHARS = 200_000

# Pinned, never @latest: `npx -y` executes whatever the registry serves, so an
# unpinned spec runs new third-party code on every call. 0.25.1 (2026-10-04)
# clears the repo's two-week cooldown; bump deliberately, like any dependency.
_SUMMARIZE_PACKAGE = "@steipete/summarize@0.25.1"


@dataclass
class LinkContent:
    """Extracted text for one user link, or the reason there is none."""

    url: str
    text: str = ""
    kind: str = "article"  # article | transcript
    via: str = ""  # which extractor produced it
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


def is_youtube_url(url: str) -> bool:
    """True for watch/short/embed links on any YouTube host."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _YOUTUBE_HOSTS


def youtube_video_id(url: str) -> str | None:
    """Extract the 11-character video id from a YouTube URL, if present."""
    if not is_youtube_url(url):
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
        candidate = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""
    else:
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    return candidate if re.fullmatch(r"[\w-]{11}", candidate) else None


def _summarize_command() -> list[str] | None:
    """How to invoke ``summarize``, or ``None`` when it cannot be run.

    Prefers an installed binary; falls back to ``npx -y`` so a YouTube link
    works on a machine with Node and no global install (verified live: the
    npx path extracts YouTube content with no API key). The first npx run
    downloads the package, which is why these calls get the CLI budget.
    """
    installed = shutil.which("summarize")
    if installed:
        return [installed]
    if shutil.which("npx"):
        return ["npx", "-y", _SUMMARIZE_PACKAGE]
    return None


def _fetch_via_summarize(command: list[str], url: str) -> LinkContent:
    """Extract clean text with the ``summarize`` CLI (no API key needed)."""
    kind = "transcript" if is_youtube_url(url) else "article"
    proc = run_subprocess(
        [*command, url, "--extract", "--plain"],
        timeout=cli_harness_timeout(CLI_QUICK_TIMEOUT),
    )
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[:300]
        return LinkContent(
            url=url, kind=kind, via="summarize", error=f"summarize failed: {detail}"
        )
    return LinkContent(
        url=url, text=proc.stdout.strip()[:_MAX_CHARS], kind=kind, via="summarize"
    )


def fetch_link(url: str) -> LinkContent:
    """Best available extraction of ``url``; never raises."""
    try:
        command = _summarize_command()
        if command:
            content = _fetch_via_summarize(command, url)
            if content.ok or is_youtube_url(url):
                # A failed YouTube extraction is terminal: the generic fetcher
                # below cannot do better on a JS-rendered player page.
                return content
        elif is_youtube_url(url):
            return LinkContent(
                url=url,
                kind="transcript",
                error=(
                    "YouTube links need Node (for `npx @steipete/summarize`) "
                    "or the summarize CLI installed; the page carries no "
                    "transcript on its own"
                ),
            )
        text, final_url = safe_fetch_text(url, max_bytes=_MAX_CHARS)
        if not text or not text.strip():
            return LinkContent(url=url, via="fetch", error="no readable text at URL")
        return LinkContent(
            url=final_url or url, text=text.strip()[:_MAX_CHARS], via="fetch"
        )
    except Exception as e:
        logger.warning("user link fetch failed for %s: %s", url, e)
        return LinkContent(url=url, error=" ".join(str(e).split())[:300])
