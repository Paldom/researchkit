"""Tests for the materials download module."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import researchkit.materials as materials
from researchkit.materials import (
    collect_source_refs,
    download_materials,
    extract_readable_text,
)
from researchkit.project import Project, ProjectConfig


def _result_payload() -> dict[str, Any]:
    return {
        "topic": "test topic",
        "provider_results": [
            {
                "provider": "openai",
                "sources": [
                    {
                        "url": "https://a.test/page",
                        "title": "A Page",
                        "source_type": "web",
                    },
                    {
                        "url": "https://b.test/post#frag",
                        "title": "B Post",
                        "source_type": "social",
                    },
                ],
            },
            {
                "provider": "grok",
                "sources": [
                    # duplicate of A (fragment differs -> same normalized URL)
                    {
                        "url": "https://a.test/page#section",
                        "title": "",
                        "source_type": "web",
                    },
                    {
                        "url": "ftp://weird.test/file",
                        "title": "FTP thing",
                        "source_type": "web",
                    },
                ],
            },
        ],
        "site_research": {
            "items_by_site": {
                "exa": [{"url": "https://c.test/doc", "title": "C Doc"}],
            }
        },
    }


def _make_project(tmp_path: Path, payload: dict[str, Any] | None = None) -> Project:
    folder = tmp_path / "20260705_test_topic"
    folder.mkdir(exist_ok=True)
    if payload is not None:
        (folder / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return Project(
        path=folder,
        config=ProjectConfig(topic="test topic"),
        created_at=datetime(2026, 7, 5),
    )


class TestCollectSourceRefs:
    def test_dedupes_and_merges_provider_attribution(self) -> None:
        refs = collect_source_refs(_result_payload())
        urls = [r.url for r in refs]
        # URLs are canonicalized: fragments and tracking params dropped.
        assert urls == [
            "https://a.test/page",
            "https://b.test/post",
            "ftp://weird.test/file",
            "https://c.test/doc",
        ]
        a = refs[0]
        assert a.providers == ["openai", "grok"]
        assert a.title == "A Page"
        assert refs[3].providers == ["site:exa"]

    def test_empty_result_yields_no_refs(self) -> None:
        assert collect_source_refs({}) == []


class TestExtractReadableText:
    def test_strips_chrome_and_keeps_headings(self) -> None:
        html = (
            "<html><head><title>The  Title</title><style>p{}</style></head>"
            "<body><script>evil()</script><h2>Section</h2>"
            "<p>Hello <b>world</b></p><noscript>nope</noscript></body></html>"
        )
        title, text = extract_readable_text(html)
        assert title == "The Title"
        assert "## Section" in text
        assert "Hello" in text and "world" in text
        assert "evil" not in text and "nope" not in text and "p{}" not in text

    def test_plain_text_passthrough(self) -> None:
        title, text = extract_readable_text("just plain text\nno markup")
        assert title == ""
        assert text == "just plain text\nno markup"


class TestDownloadMaterials:
    def _fetch_ok(self, url: str, **kwargs: Any) -> tuple[str | None, str]:
        filler = "Substantive paragraph text. " * 10  # clear the empty-page threshold
        return (
            f"<html><head><title>T {url}</title></head>"
            f"<body><p>Body of {url}</p><p>{filler}</p></body></html>",
            url,
        )

    def test_happy_path_writes_files_and_manifest(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, _result_payload())
        with patch.object(
            materials, "safe_fetch_text", side_effect=self._fetch_ok
        ) as fetch:
            manifest = download_materials(project, delay=0)

        assert manifest["fetched"] == 3  # a, b, c (ftp skipped)
        assert manifest["total_cited"] == 4
        assert fetch.call_count == 3

        statuses = {e["url"]: e["status"] for e in manifest["entries"]}
        assert statuses["ftp://weird.test/file"] == "skipped_scheme"

        materials_dir = project.path / "materials"
        files = sorted(p.name for p in materials_dir.glob("*.md"))
        assert len(files) == 3
        body = (materials_dir / files[0]).read_text(encoding="utf-8")
        assert body.startswith("---\n")
        assert "url: https://a.test/page" in body
        assert "providers: openai, grok" in body
        assert "topic: test topic" in body
        assert "Body of https://a.test/page" in body

        saved = json.loads((materials_dir / "index.json").read_text(encoding="utf-8"))
        assert saved["fetched"] == 3

    def test_failed_and_binary_sources_recorded_not_raised(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "topic": "t",
            "provider_results": [
                {
                    "provider": "openai",
                    "sources": [
                        {"url": "https://dead.test/x", "title": "Dead"},
                        {"url": "https://pdf.test/y", "title": "PDF"},
                        {"url": "http://127.0.0.1/secret", "title": "SSRF"},
                    ],
                }
            ],
        }
        project = _make_project(tmp_path, payload)

        def fetch(url: str, **kwargs: Any) -> tuple[str | None, str]:
            if "dead" in url or "127.0.0.1" in url:
                return None, url  # safe_fetch_text refusal path
            return "%PDF-1.7 binarybytes", url

        with patch.object(materials, "safe_fetch_text", side_effect=fetch):
            manifest = download_materials(project, delay=0)

        statuses = {e["url"]: e["status"] for e in manifest["entries"]}
        assert statuses["https://dead.test/x"] == "failed"
        assert statuses["https://pdf.test/y"] == "binary"
        assert statuses["http://127.0.0.1/secret"] == "failed"
        assert manifest["fetched"] == 0
        assert not list((project.path / "materials").glob("*.md"))

    def test_limit_marks_remainder_skipped(self, tmp_path: Path) -> None:
        payload = {
            "topic": "t",
            "provider_results": [
                {
                    "provider": "openai",
                    "sources": [
                        {"url": f"https://s{i}.test/p", "title": f"S{i}"}
                        for i in range(5)
                    ],
                }
            ],
        }
        project = _make_project(tmp_path, payload)
        with patch.object(materials, "safe_fetch_text", side_effect=self._fetch_ok):
            manifest = download_materials(project, limit=2, delay=0)
        counts: dict[str, int] = {}
        for e in manifest["entries"]:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        assert counts == {"fetched": 2, "skipped_limit": 3}

    def test_rerun_is_idempotent_without_refresh(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, _result_payload())
        with patch.object(
            materials, "safe_fetch_text", side_effect=self._fetch_ok
        ) as f1:
            download_materials(project, delay=0)
        assert f1.call_count == 3
        with patch.object(
            materials, "safe_fetch_text", side_effect=self._fetch_ok
        ) as f2:
            manifest = download_materials(project, delay=0)
        assert f2.call_count == 0  # existing files reused
        assert manifest["fetched"] == 3
        with patch.object(
            materials, "safe_fetch_text", side_effect=self._fetch_ok
        ) as f3:
            download_materials(project, delay=0, refresh=True)
        assert f3.call_count == 3

    def test_missing_result_json_raises(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, payload=None)
        with pytest.raises(FileNotFoundError, match="run the project"):
            download_materials(project, delay=0)

    def test_unicode_title_yields_safe_filename(self, tmp_path: Path) -> None:
        payload = {
            "topic": "t",
            "provider_results": [
                {
                    "provider": "openai",
                    "sources": [
                        {"url": "https://u.test/é", "title": "Crème Brûlée — recept 🍮"}
                    ],
                }
            ],
        }
        project = _make_project(tmp_path, payload)
        with patch.object(materials, "safe_fetch_text", side_effect=self._fetch_ok):
            manifest = download_materials(project, delay=0)
        name = manifest["entries"][0]["file"]
        assert name.endswith(".md") and " " not in name and "é" not in name
        assert (project.path / "materials" / name).is_file()


class TestUrlHygiene:
    def test_tracking_params_do_not_defeat_dedupe(self) -> None:
        payload = {
            "topic": "t",
            "provider_results": [
                {
                    "provider": "openai",
                    "sources": [
                        {"url": "https://a.test/p/?utm_source=openai", "title": "A"},
                    ],
                },
                {
                    "provider": "grok",
                    "sources": [{"url": "https://a.test/p/", "title": "A"}],
                },
            ],
        }
        refs = collect_source_refs(payload)
        assert len(refs) == 1
        assert refs[0].providers == ["openai", "grok"]

    def test_reddit_fetch_rewrites_to_old_reddit(self) -> None:
        assert (
            materials._fetch_url(
                "https://www.reddit.com/r/x/comments/1/post/?utm_source=openai"
            )
            == "https://old.reddit.com/r/x/comments/1/post/"
        )
        assert materials._fetch_url("https://a.test/p?q=1") == "https://a.test/p?q=1"

    def test_js_shell_pages_recorded_as_empty(self, tmp_path: Path) -> None:
        payload = {
            "topic": "t",
            "provider_results": [
                {
                    "provider": "grok",
                    "sources": [{"url": "https://x.test/status/1", "title": "X"}],
                }
            ],
        }
        project = _make_project(tmp_path, payload)

        def fetch(url: str, **kwargs: Any) -> tuple[str | None, str]:
            return "<html><body><div>Loading…</div></body></html>", url

        with patch.object(materials, "safe_fetch_text", side_effect=fetch):
            manifest = download_materials(project, delay=0)
        assert manifest["entries"][0]["status"] == "empty"
        assert manifest["fetched"] == 0


def test_negative_limit_means_unlimited(tmp_path: Path) -> None:
    payload = {
        "topic": "t",
        "provider_results": [
            {
                "provider": "openai",
                "sources": [
                    {"url": f"https://n{i}.test/p", "title": f"N{i}"} for i in range(3)
                ],
            }
        ],
    }
    project = _make_project(tmp_path, payload)
    filler = "Substantive paragraph text. " * 10

    def fetch(url: str, **kwargs: Any) -> tuple[str | None, str]:
        return f"<html><body><p>{filler}</p></body></html>", url

    with patch.object(materials, "safe_fetch_text", side_effect=fetch):
        manifest = download_materials(project, limit=-5, delay=0)
    assert manifest["fetched"] == 3


def test_frontmatter_url_is_canonical_across_utm_variants(tmp_path: Path) -> None:
    # Two runs citing utm-tagged vs clean variants must hand downstream
    # consumers (brainkit) the SAME url string, or the brain forks the note.
    payload = {
        "topic": "t",
        "provider_results": [
            {
                "provider": "openai",
                "sources": [
                    {"url": "https://a.test/Page?utm_source=openai#frag", "title": "A"}
                ],
            }
        ],
    }
    refs = collect_source_refs(payload)
    assert refs[0].url == "https://a.test/Page"


def test_limit_bounds_attempts_not_successes(tmp_path: Path) -> None:
    # 3 dead URLs with limit=2: only 2 network attempts may happen, the rest
    # are skipped_limit — dead links must not cause unbounded fetching.
    payload = {
        "topic": "t",
        "provider_results": [
            {
                "provider": "openai",
                "sources": [
                    {"url": f"https://dead{i}.test/x", "title": f"D{i}"}
                    for i in range(3)
                ],
            }
        ],
    }
    project = _make_project(tmp_path, payload)
    with patch.object(
        materials, "safe_fetch_text", side_effect=lambda url, **kw: (None, url)
    ) as fetch:
        manifest = download_materials(project, limit=2, delay=0)
    assert fetch.call_count == 2
    statuses = [e["status"] for e in manifest["entries"]]
    assert statuses == ["failed", "failed", "skipped_limit"]
    assert manifest["attempted"] == 2


def test_stale_file_with_wrong_url_is_refetched(tmp_path: Path) -> None:
    payload = {
        "topic": "t",
        "provider_results": [
            {
                "provider": "openai",
                "sources": [{"url": "https://real.test/a", "title": "Real"}],
            }
        ],
    }
    project = _make_project(tmp_path, payload)
    filler = "Substantive paragraph text. " * 10

    def fetch(url: str, **kwargs: Any) -> tuple[str | None, str]:
        return f"<html><body><p>{filler}</p></body></html>", url

    with patch.object(materials, "safe_fetch_text", side_effect=fetch):
        first = download_materials(project, delay=0)
    filename = first["entries"][0]["file"]
    stale = project.path / "materials" / filename
    stale.write_text(
        "---\nurl: https://other.test/z\n---\n\nstale body\n", encoding="utf-8"
    )

    with patch.object(materials, "safe_fetch_text", side_effect=fetch) as refetch:
        download_materials(project, delay=0)
    assert refetch.call_count == 1  # reuse rejected, content rewritten
    assert "url: https://real.test/a" in stale.read_text(encoding="utf-8")


def test_junk_titles_replaced_with_url_derived(tmp_path: Path) -> None:
    payload = {
        "topic": "t",
        "provider_results": [
            {
                "provider": "gemini",
                "sources": [
                    {
                        "url": "https://www.reddit.com/r/LocalLLaMA/comments/1x/best_local_agents/",
                        "title": "Reddit - Please wait for verification",
                        "date": "2026-07-01",
                    }
                ],
            }
        ],
    }
    project = _make_project(tmp_path, payload)
    filler = "Substantive paragraph text. " * 10
    body = f"<html><head><title>Just a moment...</title></head><body><p>{filler}</p></body></html>"

    with patch.object(
        materials, "safe_fetch_text", side_effect=lambda url, **kw: (body, url)
    ):
        manifest = download_materials(project, delay=0)

    entry = manifest["entries"][0]
    assert entry["title"] == "best local agents (reddit.com)"
    note = (project.path / "materials" / entry["file"]).read_text(encoding="utf-8")
    assert "title: best local agents (reddit.com)" in note
    assert "published: 2026-07-01" in note


def test_connector_content_is_archived_without_fetch(tmp_path: Path) -> None:
    payload = {
        "topic": "t",
        "provider_results": [],
        "site_research": {
            "items_by_site": {
                "medium": [
                    {
                        "url": "https://medium.com/@a/post",
                        "title": "Deep Post",
                        "content": "Full article body " * 30,
                        "content_kind": "article",
                    }
                ],
                "youtube": [
                    {
                        "url": "https://www.youtube.com/watch?v=abc123def",
                        "title": "Talk",
                        "content": "transcript line\n" * 40,
                        "content_kind": "transcript",
                    }
                ],
            }
        },
    }
    project = _make_project(tmp_path, payload)
    with patch.object(materials, "safe_fetch_text") as fetch:
        manifest = download_materials(project, delay=0)

    assert fetch.call_count == 0  # never re-queried
    origins = {e["url"]: e.get("origin") for e in manifest["entries"]}
    assert origins["https://medium.com/@a/post"] == "connector:article"
    assert (
        origins["https://www.youtube.com/watch?v=abc123def"] == "connector:transcript"
    )
    assert manifest["fetched"] == 2

    files = {e["url"]: e["file"] for e in manifest["entries"]}
    medium_note = (
        project.path / "materials" / files["https://medium.com/@a/post"]
    ).read_text(encoding="utf-8")
    assert "content_kind: article" in medium_note
    assert "Full article body" in medium_note


def test_summary_fallback_when_fetch_fails_or_empty(tmp_path: Path) -> None:
    summary = {
        "tldr": ["point one"],
        "key_takeaways": ["do the thing"],
        "key_quotes": ["a quote"],
    }
    payload = {
        "topic": "t",
        "provider_results": [],
        "site_research": {
            "items_by_site": {
                "youtube": [
                    {
                        "url": "https://yt.test/dead",
                        "title": "Dead",
                        "summary": summary,
                    },
                    {
                        "url": "https://yt.test/shell",
                        "title": "Shell",
                        "summary": summary,
                    },
                    {"url": "https://yt.test/none", "title": "NoSummary"},
                ]
            }
        },
    }
    project = _make_project(tmp_path, payload)

    def fetch(url: str, **kwargs: Any) -> tuple[str | None, str]:
        if "dead" in url:
            return None, url
        return "<html><body><div>Loading…</div></body></html>", url

    with patch.object(materials, "safe_fetch_text", side_effect=fetch):
        manifest = download_materials(project, delay=0)

    rows = {e["url"]: e for e in manifest["entries"]}
    assert rows["https://yt.test/dead"]["origin"] == "summary"
    assert rows["https://yt.test/shell"]["origin"] == "summary"
    assert rows["https://yt.test/none"]["status"] == "empty"

    body = (
        project.path / "materials" / rows["https://yt.test/dead"]["file"]
    ).read_text(encoding="utf-8")
    assert "content_kind: summary" in body
    assert "## TL;DR" in body and "> a quote" in body


def test_http_and_cached_origins_recorded(tmp_path: Path) -> None:
    payload = {
        "topic": "t",
        "provider_results": [
            {
                "provider": "openai",
                "sources": [{"url": "https://h.test/p", "title": "H"}],
            }
        ],
    }
    project = _make_project(tmp_path, payload)
    filler = "Substantive paragraph text. " * 10

    def fetch(url: str, **kwargs: Any) -> tuple[str | None, str]:
        return f"<html><body><p>{filler}</p></body></html>", url

    with patch.object(materials, "safe_fetch_text", side_effect=fetch):
        first = download_materials(project, delay=0)
    assert first["entries"][0]["origin"] == "http"
    with patch.object(materials, "safe_fetch_text", side_effect=fetch):
        second = download_materials(project, delay=0)
    assert second["entries"][0]["origin"] == "cached"
