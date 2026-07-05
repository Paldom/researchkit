"""Tests for user-curated sources (project config, project ops, formatter, service)."""

from __future__ import annotations

from pathlib import Path

import pytest

from researchkit.aggregator import InsightBundle
from researchkit.formatter import format_as_markdown
from researchkit.project import (
    ProjectConfig,
    UserFileSource,
    UserUrlSource,
    create_project,
    load_project,
)
from researchkit.providers.base import ProviderResult
from researchkit.service import SocialResearchService


def _make_config(**overrides) -> ProjectConfig:
    base = {"topic": "AI agents"}
    base.update(overrides)
    return ProjectConfig(**base)


class TestProjectConfigRoundTrip:
    def test_default_config_omits_user_source_keys(self) -> None:
        cfg = _make_config()
        d = cfg.to_dict()
        assert "user_url_sources" not in d
        assert "user_file_sources" not in d

    def test_round_trip_preserves_user_sources(self) -> None:
        cfg = _make_config(
            user_url_sources=[
                UserUrlSource(url="https://example.com", title="Ex", note="ref"),
                UserUrlSource(url="https://example.org/post"),
            ],
            user_file_sources=[
                UserFileSource(filename="notes.md", title="Notes"),
            ],
        )
        restored = ProjectConfig.from_dict(cfg.to_dict())
        assert len(restored.user_url_sources) == 2
        assert restored.user_url_sources[0].url == "https://example.com"
        assert restored.user_url_sources[0].title == "Ex"
        assert restored.user_url_sources[0].note == "ref"
        assert restored.user_file_sources[0].filename == "notes.md"
        assert restored.user_file_sources[0].title == "Notes"

    def test_old_config_loads_with_empty_user_sources(self) -> None:
        old_data = {"topic": "Old project"}
        cfg = ProjectConfig.from_dict(old_data)
        assert cfg.user_url_sources == []
        assert cfg.user_file_sources == []


class TestProjectFileOps:
    def test_add_file_source_copies_into_user_sources_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "external.md"
        src.write_text("# external", encoding="utf-8")

        project = create_project(_make_config(), projects_dir=tmp_path / "projects")
        entry = project.add_user_file_source(src, title="External")

        assert entry.filename == "external.md"
        copied = project.user_sources_dir / "external.md"
        assert copied.exists()
        assert copied.read_text(encoding="utf-8") == "# external"
        assert project.config.user_file_sources[0].title == "External"

    def test_filename_collisions_are_suffixed(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "notes.md").write_text("first", encoding="utf-8")
        (b / "notes.md").write_text("second", encoding="utf-8")

        project = create_project(_make_config(), projects_dir=tmp_path / "projects")
        e1 = project.add_user_file_source(a / "notes.md")
        e2 = project.add_user_file_source(b / "notes.md")

        assert e1.filename == "notes.md"
        assert e2.filename == "notes_1.md"
        assert (project.user_sources_dir / "notes.md").read_text(
            encoding="utf-8"
        ) == "first"
        assert (project.user_sources_dir / "notes_1.md").read_text(
            encoding="utf-8"
        ) == "second"

    def test_remove_file_source_deletes_from_disk(self, tmp_path: Path) -> None:
        src = tmp_path / "doc.md"
        src.write_text("hello", encoding="utf-8")

        project = create_project(_make_config(), projects_dir=tmp_path / "projects")
        project.add_user_file_source(src)
        on_disk = project.user_sources_dir / "doc.md"
        assert on_disk.exists()

        assert project.remove_user_source("doc.md") is True
        assert not on_disk.exists()
        assert project.config.user_file_sources == []

    def test_url_dedup_is_case_insensitive(self, tmp_path: Path) -> None:
        project = create_project(_make_config(), projects_dir=tmp_path / "projects")
        assert (
            project.add_user_url_source(UserUrlSource(url="https://Example.com"))
            is True
        )
        assert (
            project.add_user_url_source(UserUrlSource(url="https://example.com"))
            is False
        )
        assert len(project.config.user_url_sources) == 1


class TestServiceAddRemoveList:
    def test_add_remove_list_url(self, tmp_path: Path) -> None:
        svc = SocialResearchService(projects_dir=tmp_path / "projects")
        project = svc.create_project(topic="t")

        added = svc.add_user_source(
            project, "https://example.com", title="Ex", note="ref"
        )
        assert isinstance(added, UserUrlSource)

        urls, files = svc.list_user_sources(project)
        assert len(urls) == 1
        assert urls[0].title == "Ex"
        assert files == []

        # Persisted to disk
        reloaded = load_project(project.path)
        assert len(reloaded.config.user_url_sources) == 1

        assert svc.remove_user_source(project, "https://example.com") is True
        assert svc.list_user_sources(project) == ([], [])

    def test_invalid_url_rejected(self, tmp_path: Path) -> None:
        svc = SocialResearchService(projects_dir=tmp_path / "projects")
        project = svc.create_project(topic="t")
        with pytest.raises(ValueError):
            svc.add_user_source(project, "ftp://example.com")

    def test_add_file_source_via_service(self, tmp_path: Path) -> None:
        svc = SocialResearchService(projects_dir=tmp_path / "projects")
        project = svc.create_project(topic="t")
        src = tmp_path / "input.md"
        src.write_text("body", encoding="utf-8")

        added = svc.add_user_source(project, str(src), title="In", note="n")
        assert isinstance(added, UserFileSource)
        assert added.title == "In"
        _urls, files = svc.list_user_sources(project)
        assert len(files) == 1
        assert (project.user_sources_dir / "input.md").exists()


def _bundle_with_user_sources(
    *,
    urls: list[UserUrlSource] | None = None,
    files: list[UserFileSource] | None = None,
) -> InsightBundle:
    return InsightBundle(
        topic="AI agents",
        keywords=[],
        days=7,
        providers_queried=["openai"],
        meta_summary="Meta",
        provider_results=[
            ProviderResult(provider="openai", model="m", raw_text="x"),
        ],
        individual_summaries={"openai": "summary"},
        user_url_sources=urls or [],
        user_file_sources=files or [],
    )


class TestFormatterRenderUserSources:
    def test_renders_url_section_with_note(self) -> None:
        md = format_as_markdown(
            _bundle_with_user_sources(
                urls=[UserUrlSource(url="https://ex.com", title="Ex", note="why")]
            ),
            include_raw=False,
        )
        assert "## User-Curated Sources" in md
        assert "[Ex](https://ex.com)" in md
        assert "— why" in md

    def test_renders_file_section_without_url_treatment(self) -> None:
        md = format_as_markdown(
            _bundle_with_user_sources(
                files=[UserFileSource(filename="notes.md", title="My Notes")]
            ),
            include_raw=False,
        )
        assert "## User-Curated Sources" in md
        assert "User-Provided Documents" in md
        assert "context only, not citations" in md
        # The file should not be rendered as a clickable link.
        assert "(notes.md)" not in md.replace("`notes.md`", "")
        assert "My Notes" in md

    def test_section_omitted_when_no_user_sources(self) -> None:
        md = format_as_markdown(
            _bundle_with_user_sources(),
            include_raw=False,
        )
        assert "## User-Curated Sources" not in md
        assert "User-Provided Documents" not in md


class TestBundleSerialization:
    def test_to_dict_excludes_file_contents(self) -> None:
        bundle = _bundle_with_user_sources(
            urls=[UserUrlSource(url="https://ex.com")],
            files=[UserFileSource(filename="a.md")],
        )
        bundle.user_file_contents["a.md"] = "secret content"
        d = bundle.to_dict()
        assert "user_url_sources" in d
        assert "user_file_sources" in d
        # Contents must not be serialized to result.json.
        assert "user_file_contents" not in d
        assert "secret content" not in str(d)
