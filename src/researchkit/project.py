"""
Project management for Social Research.

A project is a folder containing:
- config.json: The research configuration
- result.json: The research results (after run)
- report.md: The formatted report (after run)
- run.log: The run log (after run)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from researchkit.safe_io import atomic_write_text, safe_unlink_within
from researchkit.utils import slugify

logger = logging.getLogger(__name__)

PROJECTS_DIR = Path("projects")
CONFIG_FILENAME = "config.json"
RESULT_FILENAME = "result.json"
REPORT_FILENAME = "report.md"
LOG_FILENAME = "run.log"
USER_SOURCES_DIRNAME = "user_sources"
# Boost mode: a parent project nests its sub-projects under this directory and
# writes the opus-authored cross-cutting synthesis to SUPER_SUMMARY_FILENAME.
SUBPROJECTS_DIRNAME = "subprojects"
SUPER_SUMMARY_FILENAME = "super_summary.md"


@dataclass
class UserUrlSource:
    """A user-supplied URL to be cited in the final article."""

    url: str
    title: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"url": self.url}
        if self.title:
            result["title"] = self.title
        if self.note:
            result["note"] = self.note
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserUrlSource:
        return cls(
            url=data["url"],
            title=data.get("title"),
            note=data.get("note"),
        )


@dataclass
class UserFileSource:
    """A user-supplied document used as context for the final article only."""

    filename: str  # Relative to projects/<name>/user_sources/
    title: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"filename": self.filename}
        if self.title:
            result["title"] = self.title
        if self.note:
            result["note"] = self.note
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserFileSource:
        # Persist only the basename so a hand-edited / shared config.json can't
        # smuggle a traversal path (e.g. "../../secret"). (Review S1.)
        return cls(
            filename=Path(str(data["filename"])).name,
            title=data.get("title"),
            note=data.get("note"),
        )


@dataclass
class ProjectConfig:
    """Research project configuration."""

    topic: str
    keywords: list[str] = field(
        default_factory=list
    )  # Search keywords to guide research
    days: int = 7
    providers: list[str] = field(
        default_factory=lambda: ["openai", "gemini", "grok", "perplexity"]
    )
    sources: list[str] = field(default_factory=lambda: ["social", "web"])
    include_raw: bool = True
    preset_name: str | None = None  # Model preset to use (None = active preset)
    site_research_enabled: bool = True  # Enable keyword-based site research
    # empty = all active connectors at run time (registry-driven)
    site_research_sites: list[str] = field(default_factory=list)
    # User-curated sources, included in final report/article only (not in research).
    user_url_sources: list[UserUrlSource] = field(default_factory=list)
    user_file_sources: list[UserFileSource] = field(default_factory=list)
    # Boost mode: set on sub-projects so a run knows it is one step of a larger,
    # parallel investigation (used to inject sibling-awareness into research prompts).
    parent_topic: str | None = None
    sibling_topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "topic": self.topic,
            "keywords": self.keywords,
            "days": self.days,
            "providers": self.providers,
            "sources": self.sources,
            "include_raw": self.include_raw,
            "site_research_enabled": self.site_research_enabled,
            "site_research_sites": self.site_research_sites,
        }
        # Include preset_name if set
        if self.preset_name is not None:
            result["preset_name"] = self.preset_name
        # Boost/sub-project metadata, only when present (backward compatible).
        if self.parent_topic is not None:
            result["parent_topic"] = self.parent_topic
        if self.sibling_topics:
            result["sibling_topics"] = self.sibling_topics
        # Only emit user-source fields when present, so old result.json
        # consumers and old configs are unaffected.
        if self.user_url_sources:
            result["user_url_sources"] = [s.to_dict() for s in self.user_url_sources]
        if self.user_file_sources:
            result["user_file_sources"] = [s.to_dict() for s in self.user_file_sources]
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectConfig:
        """Create from dictionary."""
        return cls(
            topic=data["topic"],
            keywords=data.get("keywords", []),
            days=data.get("days", 7),
            providers=data.get("providers", ["openai", "gemini", "grok", "perplexity"]),
            sources=data.get("sources", ["social", "web"]),
            include_raw=data.get("include_raw", True),
            preset_name=data.get("preset_name"),
            site_research_enabled=data.get("site_research_enabled", True),
            site_research_sites=data.get("site_research_sites", []),
            user_url_sources=[
                UserUrlSource.from_dict(s) for s in data.get("user_url_sources", [])
            ],
            user_file_sources=[
                UserFileSource.from_dict(s) for s in data.get("user_file_sources", [])
            ],
            parent_topic=data.get("parent_topic"),
            sibling_topics=data.get("sibling_topics", []),
        )


@dataclass
class Project:
    """A research project with its folder and configuration."""

    path: Path
    config: ProjectConfig
    created_at: datetime

    @property
    def config_path(self) -> Path:
        return self.path / CONFIG_FILENAME

    @property
    def result_path(self) -> Path:
        return self.path / RESULT_FILENAME

    @property
    def report_path(self) -> Path:
        return self.path / REPORT_FILENAME

    @property
    def log_path(self) -> Path:
        return self.path / LOG_FILENAME

    @property
    def user_sources_dir(self) -> Path:
        return self.path / USER_SOURCES_DIRNAME

    @property
    def subprojects_dir(self) -> Path:
        return self.path / SUBPROJECTS_DIRNAME

    @property
    def super_summary_path(self) -> Path:
        return self.path / SUPER_SUMMARY_FILENAME

    @property
    def is_subproject(self) -> bool:
        """True when this project was spawned as a sub-query of a boosted run."""
        return self.config.parent_topic is not None

    def list_subprojects(self) -> list[Project]:
        """Load the sub-projects nested under this project (boost mode)."""
        if not self.subprojects_dir.exists():
            return []
        subs: list[Project] = []
        for folder in sorted(self.subprojects_dir.iterdir()):
            if folder.is_dir() and (folder / CONFIG_FILENAME).exists():
                try:
                    subs.append(load_project(folder))
                except Exception as e:
                    logger.warning(f"Failed to load sub-project {folder}: {e}")
        return subs

    def save_super_summary(self, markdown: str) -> None:
        """Save the cross-cutting super-summary markdown."""
        self.super_summary_path.write_text(markdown, encoding="utf-8")
        logger.info(f"Saved super-summary to {self.super_summary_path}")

    @property
    def name(self) -> str:
        return self.path.name

    def summary(self) -> dict[str, Any]:
        """Serializable listing entry (shared by the API and MCP servers)."""
        return {
            "name": self.name,
            "topic": self.config.topic,
            "days": self.config.days,
            "providers": list(self.config.providers),
            "created_at": self.created_at.isoformat(),
            "has_report": self.report_path.is_file(),
        }

    @property
    def has_results(self) -> bool:
        return self.result_path.exists()

    def add_user_url_source(self, source: UserUrlSource) -> bool:
        """
        Add a URL source. Returns False if a duplicate URL already exists.

        Duplicates are detected case-insensitively on the full URL string.
        """
        existing = {s.url.lower() for s in self.config.user_url_sources}
        if source.url.lower() in existing:
            return False
        self.config.user_url_sources.append(source)
        return True

    def add_user_file_source(
        self,
        src_path: Path,
        title: str | None = None,
        note: str | None = None,
    ) -> UserFileSource:
        """
        Copy a file into the project's user_sources/ directory and register it.

        On filename collision, append a numeric suffix to the stem.
        """
        import shutil

        if not src_path.exists() or not src_path.is_file():
            raise FileNotFoundError(f"User source file not found: {src_path}")

        self.user_sources_dir.mkdir(parents=True, exist_ok=True)

        target_name = src_path.name
        target = self.user_sources_dir / target_name
        n = 1
        while target.exists():
            target_name = f"{src_path.stem}_{n}{src_path.suffix}"
            target = self.user_sources_dir / target_name
            n += 1

        shutil.copy2(src_path, target)
        entry = UserFileSource(filename=target_name, title=title, note=note)
        self.config.user_file_sources.append(entry)
        return entry

    def remove_user_source(self, identifier: str) -> bool:
        """
        Remove a user URL or file source by URL, filename, or 1-based index.

        For file sources, also delete the copied file from user_sources/.
        Returns True if a removal occurred.
        """
        # Try 1-based index across the combined list (urls first, then files).
        # `isascii()` guard: str.isdigit() is True for unicode digits like "²"
        # that int() then rejects with ValueError. (Review project.py:277.)
        if identifier.isascii() and identifier.isdigit():
            idx = int(identifier)
            urls = self.config.user_url_sources
            files = self.config.user_file_sources
            combined = len(urls) + len(files)
            if 1 <= idx <= combined:
                if idx <= len(urls):
                    urls.pop(idx - 1)
                    return True
                file_idx = idx - len(urls) - 1
                removed = files.pop(file_idx)
                self._delete_user_file(removed.filename)
                return True

        # Match URL.
        for i, s in enumerate(self.config.user_url_sources):
            if s.url == identifier:
                self.config.user_url_sources.pop(i)
                return True

        # Match filename.
        for i, f in enumerate(self.config.user_file_sources):
            if f.filename == identifier:
                self.config.user_file_sources.pop(i)
                self._delete_user_file(f.filename)
                return True

        return False

    def _delete_user_file(self, filename: str) -> None:
        """Delete a copied user-source file, refusing any path outside user_sources/.

        ``filename`` originates from config.json, so a tampered/shared project
        could carry a traversal path; safe_unlink_within raises on those rather
        than deleting an out-of-tree file. (Review S1.)
        """
        try:
            safe_unlink_within(self.user_sources_dir, filename)
        except (OSError, ValueError) as e:
            logger.warning(
                f"Refusing/failed to delete user source file {filename!r}: {e}"
            )

    def save_config(self) -> None:
        """Save the configuration to config.json."""
        data = {
            "created_at": self.created_at.isoformat(),
            **self.config.to_dict(),
        }
        atomic_write_text(self.config_path, json.dumps(data, indent=2))
        logger.info(f"Saved config to {self.config_path}")

    def save_results(self, result_json: dict[str, Any], report_markdown: str) -> None:
        """Save the results and report atomically (crash mid-write can't corrupt them)."""
        atomic_write_text(self.result_path, json.dumps(result_json, indent=2))
        atomic_write_text(self.report_path, report_markdown)
        logger.info(f"Saved results to {self.result_path}")
        logger.info(f"Saved report to {self.report_path}")

    def load_results(self) -> dict[str, Any] | None:
        """Load results if they exist."""
        if not self.result_path.exists():
            return None
        return json.loads(self.result_path.read_text(encoding="utf-8"))

    def load_report(self) -> str | None:
        """Load report if it exists."""
        if not self.report_path.exists():
            return None
        return self.report_path.read_text(encoding="utf-8")


def generate_project_name(topic: str, timestamp: datetime | None = None) -> str:
    """
    Generate a project folder name like: 20251229_143052_ai_agents

    Args:
        topic: The research topic
        timestamp: Optional timestamp (defaults to now)

    Returns:
        A folder name with timestamp and slugified topic
    """
    ts = timestamp or datetime.now()
    ts_str = ts.strftime("%Y%m%d_%H%M%S")
    topic_slug = slugify(topic, max_length=40).replace("-", "_")
    return f"{ts_str}_{topic_slug}"


def _uniquify_dir(base: Path, name: str) -> Path:
    """Return ``base/name``, or ``base/name_2``, ``_3`` … if it already exists.

    Two runs of the same topic in the same second (realistic for agent/batch
    invocation, and more likely for non-ASCII topics that slugify to the same
    fallback) used to reuse one folder and clobber each other's results.
    (Review L19.)
    """
    candidate = base / name
    if not candidate.exists():
        return candidate
    for i in range(2, 1000):
        candidate = base / f"{name}_{i}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(
        f"Could not find a free project folder for {name!r} under {base}"
    )


def create_project(
    config: ProjectConfig,
    projects_dir: Path = PROJECTS_DIR,
    timestamp: datetime | None = None,
) -> Project:
    """
    Create a new project folder with configuration.

    Args:
        config: The research configuration
        projects_dir: Base directory for projects (default: projects/)
        timestamp: Optional timestamp for folder name (defaults to now)

    Returns:
        The created Project
    """
    ts = timestamp or datetime.now()
    folder_name = generate_project_name(config.topic, ts)
    projects_dir.mkdir(parents=True, exist_ok=True)
    project_path = _uniquify_dir(projects_dir, folder_name)

    # Create the project folder (unique name, so exist_ok is just belt-and-braces)
    project_path.mkdir(parents=True, exist_ok=True)

    project = Project(
        path=project_path,
        config=config,
        created_at=ts,
    )
    project.save_config()

    logger.info(f"Created project: {project_path}")
    return project


def create_subproject(
    parent: Project,
    config: ProjectConfig,
    index: int,
    timestamp: datetime | None = None,
) -> Project:
    """Create a sub-project nested under a parent project's subprojects/ dir.

    Sub-project folders are named ``sub_NN_<slug>`` (1-based, zero-padded) so they
    sort in decomposition order and stay grouped under the parent.

    Args:
        parent: The parent (boosted) project.
        config: The sub-project's research configuration.
        index: 1-based position among the sibling sub-queries.
        timestamp: Optional creation timestamp (defaults to now).
    """
    ts = timestamp or datetime.now()
    slug = slugify(config.topic, max_length=40).replace("-", "_")
    folder_name = f"sub_{index:02d}_{slug}"
    parent.subprojects_dir.mkdir(parents=True, exist_ok=True)
    project_path = _uniquify_dir(parent.subprojects_dir, folder_name)
    project_path.mkdir(parents=True, exist_ok=True)

    project = Project(path=project_path, config=config, created_at=ts)
    project.save_config()
    logger.info(f"Created sub-project: {project_path}")
    return project


def load_project(project_path: Path) -> Project:
    """
    Load an existing project from its folder.

    Args:
        project_path: Path to the project folder

    Returns:
        The loaded Project

    Raises:
        FileNotFoundError: If the project or config doesn't exist
        ValueError: If the config is invalid
    """
    config_path = project_path / CONFIG_FILENAME

    if not project_path.exists():
        raise FileNotFoundError(f"Project folder not found: {project_path}")

    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8"))

    created_at_str = data.pop("created_at", None)
    if created_at_str:
        created_at = datetime.fromisoformat(created_at_str)
    else:
        # Fallback: parse from folder name
        folder_name = project_path.name
        try:
            ts_part = folder_name[:15]  # YYYYMMDD_HHMMSS
            created_at = datetime.strptime(ts_part, "%Y%m%d_%H%M%S")
        except ValueError:
            created_at = datetime.now()

    config = ProjectConfig.from_dict(data)

    return Project(
        path=project_path,
        config=config,
        created_at=created_at,
    )


def list_projects(projects_dir: Path = PROJECTS_DIR) -> list[Project]:
    """
    List all projects in the projects directory.

    Args:
        projects_dir: Base directory for projects

    Returns:
        List of projects, sorted by creation time (newest first)
    """
    if not projects_dir.exists():
        return []

    projects = []
    for folder in projects_dir.iterdir():
        if folder.is_dir() and (folder / CONFIG_FILENAME).exists():
            try:
                project = load_project(folder)
                projects.append(project)
            except Exception as e:
                logger.warning(f"Failed to load project {folder}: {e}")

    # Sort by created_at, newest first
    projects.sort(key=lambda p: p.created_at, reverse=True)
    return projects


def find_project(
    name_or_path: str, projects_dir: Path = PROJECTS_DIR
) -> Project | None:
    """
    Find a project by name or path.

    Args:
        name_or_path: Project folder name or full path
        projects_dir: Base directory for projects

    Returns:
        The project if found, None otherwise
    """
    # Try as full path first
    path = Path(name_or_path)
    if path.exists() and (path / CONFIG_FILENAME).exists():
        return load_project(path)

    # Try as name in projects_dir
    project_path = projects_dir / name_or_path
    if project_path.exists() and (project_path / CONFIG_FILENAME).exists():
        return load_project(project_path)

    return None
