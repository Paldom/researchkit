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


def _as_fetch(fake):  # type: ignore[no-untyped-def]
    """Adapt an old-style ``safe_fetch_text`` fake ((text, final_url)) to
    ``safe_fetch`` (FetchResult): materials now reads status/MIME too."""
    from researchkit.safe_io import FetchResult

    def _fetch(url, **kw):  # type: ignore[no-untyped-def]
        text, final = fake(url, **kw)
        return FetchResult(url, final, text=text, status=200 if text is not None else 0)

    return _fetch


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
            materials, "safe_fetch", side_effect=_as_fetch(self._fetch_ok)
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

        with patch.object(materials, "safe_fetch", side_effect=_as_fetch(fetch)):
            manifest = download_materials(project, delay=0)

        statuses = {e["url"]: e["status"] for e in manifest["entries"]}
        assert statuses["https://dead.test/x"] == "failed"
        assert statuses["https://pdf.test/y"] == "pdf_unparsed"  # cited, link-only
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
        with patch.object(
            materials, "safe_fetch", side_effect=_as_fetch(self._fetch_ok)
        ):
            manifest = download_materials(project, limit=2, delay=0)
        counts: dict[str, int] = {}
        for e in manifest["entries"]:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        assert counts == {"fetched": 2, "skipped_limit": 3}

    def test_rerun_is_idempotent_without_refresh(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, _result_payload())
        with patch.object(
            materials, "safe_fetch", side_effect=_as_fetch(self._fetch_ok)
        ) as f1:
            download_materials(project, delay=0)
        assert f1.call_count == 3
        with patch.object(
            materials, "safe_fetch", side_effect=_as_fetch(self._fetch_ok)
        ) as f2:
            manifest = download_materials(project, delay=0)
        assert f2.call_count == 0  # existing files reused
        assert manifest["fetched"] == 3
        with patch.object(
            materials, "safe_fetch", side_effect=_as_fetch(self._fetch_ok)
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
        with patch.object(
            materials, "safe_fetch", side_effect=_as_fetch(self._fetch_ok)
        ):
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

        with patch.object(materials, "safe_fetch", side_effect=_as_fetch(fetch)):
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

    with patch.object(materials, "safe_fetch", side_effect=_as_fetch(fetch)):
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
        materials, "safe_fetch", side_effect=_as_fetch(lambda url, **kw: (None, url))
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

    with patch.object(materials, "safe_fetch", side_effect=_as_fetch(fetch)):
        first = download_materials(project, delay=0)
    filename = first["entries"][0]["file"]
    stale = project.path / "materials" / filename
    stale.write_text(
        "---\nurl: https://other.test/z\n---\n\nstale body\n", encoding="utf-8"
    )

    with patch.object(materials, "safe_fetch", side_effect=_as_fetch(fetch)) as refetch:
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
        materials, "safe_fetch", side_effect=_as_fetch(lambda url, **kw: (body, url))
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
    with patch.object(materials, "safe_fetch") as fetch:
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

    with patch.object(materials, "safe_fetch", side_effect=_as_fetch(fetch)):
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

    with patch.object(materials, "safe_fetch", side_effect=_as_fetch(fetch)):
        first = download_materials(project, delay=0)
    assert first["entries"][0]["origin"] == "http"
    with patch.object(materials, "safe_fetch", side_effect=_as_fetch(fetch)):
        second = download_materials(project, delay=0)
    assert second["entries"][0]["origin"] == "cached"


def test_material_frontmatter_carries_content_digest(tmp_path: Path) -> None:
    # Content lineage for downstream knowledge tools: same body -> same
    # digest; changed body -> changed digest (second dedup key beyond URL).
    from researchkit.materials import _write_material, collect_source_refs

    payload = {
        "topic": "t",
        "provider_results": [
            {
                "provider": "openai",
                "sources": [{"url": "https://d.test/a", "title": "A"}],
            }
        ],
    }
    ref = collect_source_refs(payload)[0]
    p1, p2, p3 = (tmp_path / f"{i}.md" for i in range(3))
    _write_material(p1, ref, payload, body="same body")
    _write_material(p2, ref, payload, body="same body")
    _write_material(p3, ref, payload, body="different body")

    def digest_of(path: Path) -> str:
        line = next(
            line
            for line in path.read_text().splitlines()
            if line.startswith("content_digest:")
        )
        return line.split(":", 1)[1].strip()

    assert digest_of(p1) == digest_of(p2)
    assert digest_of(p1) != digest_of(p3)
    assert len(digest_of(p1)) == 16


class TestFetchPolicy:
    """PLAN 2.3: classify the host before fetching."""

    def test_link_only_and_rejected_hosts(self) -> None:
        from researchkit.materials import fetch_policy

        assert fetch_policy("https://x.com/a/status/1") == "tos-blocked"
        assert fetch_policy("https://www.linkedin.com/posts/x") == "tos-blocked"
        assert fetch_policy("https://someone.medium.com/post") == "tos-blocked"
        assert fetch_policy("https://youtu.be/abc") == "tos-blocked"
        assert fetch_policy("https://archive.ph/abc") == "rejected"
        assert fetch_policy("https://example.com/a") is None
        assert fetch_policy("https://notx.com/a") is None  # suffix, not substring

    def test_github_repo_reads_the_readme(self) -> None:
        from researchkit.materials import _github_readme_url

        assert (
            _github_readme_url("https://github.com/openclaw/acpx")
            == "https://raw.githubusercontent.com/openclaw/acpx/HEAD/README.md"
        )
        assert _github_readme_url("https://github.com/openclaw/acpx/issues/1") is None
        assert _github_readme_url("https://gitlab.com/a/b") is None

    def test_paywalled_teaser_is_not_archived(self, tmp_path: Path) -> None:
        from researchkit.safe_io import FetchResult

        payload = {
            "topic": "t",
            "provider_results": [
                {
                    "provider": "grok",
                    "sources": [
                        {
                            "url": "https://hbr.example/teaser",
                            "title": "T",
                            "source_type": "web",
                        },
                        {
                            "url": "https://x.com/a/1",
                            "title": "X",
                            "source_type": "social",
                        },
                    ],
                }
            ],
        }
        project = _make_project(tmp_path, payload)
        teaser = (
            '<html><script type="application/ld+json">{"isAccessibleForFree": false}'
            "</script><body><article>"
            + "Teaser words here. " * 30
            + "</article></body></html>"
        )

        def fetch(url: str, **kw: object) -> FetchResult:
            return FetchResult(
                url, url, text=teaser, status=200, content_type="text/html"
            )

        with patch.object(materials, "safe_fetch", side_effect=fetch) as called:
            manifest = download_materials(project, delay=0)
        statuses = {e["url"]: e["status"] for e in manifest["entries"]}
        assert statuses["https://hbr.example/teaser"] == "paywall"
        assert statuses["https://x.com/a/1"] == "tos-blocked"
        assert called.call_count == 1  # x.com never fetched


def _text_pdf(text: str) -> bytes:
    """A minimal one-page PDF with a real text layer (no binary fixtures)."""
    stream = f"BT /F1 10 Tf 20 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return bytes(out)


class TestPdfAndMainContent:
    """PLAN 2.13 (pypdf) and 4.7 (trafilatura first, stdlib fallback)."""

    def test_pdf_text_is_archived(self, tmp_path: Path) -> None:
        from researchkit.safe_io import FetchResult

        sentence = "Grounded citation verification needs archived page text. " * 6
        pdf = _text_pdf(sentence)
        payload = {
            "topic": "t",
            "provider_results": [
                {
                    "provider": "openai",
                    "sources": [{"url": "https://pdf.test/paper.pdf"}],
                }
            ],
        }
        project = _make_project(tmp_path, payload)

        def fetch(url: str, **kw: Any) -> FetchResult:
            return FetchResult(
                url,
                url,
                text=pdf.decode("latin-1"),
                body=pdf,
                status=200,
                content_type="application/pdf",
            )

        with patch.object(materials, "safe_fetch", side_effect=fetch):
            manifest = download_materials(project, delay=0)
        [entry] = manifest["entries"]
        assert entry["status"] == "fetched" and entry["origin"] == "http:pdf"
        body = (project.path / "materials" / entry["file"]).read_text(encoding="utf-8")
        assert "Grounded citation verification needs archived page text." in body

    def test_unreadable_pdf_stays_link_only(self) -> None:
        assert materials.extract_pdf_text(b"%PDF-1.7 not really") == ("", "")

    def test_main_content_drops_navigation(self) -> None:
        article = " ".join(
            f"Paragraph {i} explains how archived pages ground every cited claim."
            for i in range(12)
        )
        html = (
            "<html><head><title>Grounding</title></head><body>"
            "<nav><a href='/'>Home</a> <a href='/login'>Sign in</a> Subscribe</nav>"
            f"<article><h1>Grounding</h1><p>{article}</p></article>"
            "<footer>Accept all cookies. Privacy policy.</footer></body></html>"
        )
        title, text = materials.extract_readable_text(html)
        assert title == "Grounding"
        assert "archived pages ground every cited claim" in text
        assert "Sign in" not in text and "Accept all cookies" not in text

    def test_short_pages_fall_back_to_the_stdlib_walk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(materials, "_main_content", lambda html: "")
        _, text = materials.extract_readable_text(
            "<html><body><p>Tiny page.</p></body></html>"
        )
        assert text == "Tiny page."
