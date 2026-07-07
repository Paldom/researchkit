"""System-level model configuration with presets.

Reads model configuration from models.yaml in the project root.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from researchkit.safe_io import atomic_write_text

logger = logging.getLogger(__name__)

MODELS_FILENAME = "models.yaml"
# Sidecar holding runtime state (the active preset). Kept out of models.yaml so
# the UI's "Apply preset" no longer rewrites the version-controlled models.yaml
# and strips its comments. (Review L21.)
ACTIVE_PRESET_STATE_FILENAME = ".active_preset.json"

# Minimal embedded config so a non-editable install with no models.yaml on disk
# starts with sane, non-deep defaults instead of crashing at import. (Review M16.)
_EMBEDDED_DEFAULT_CONFIG: dict[str, Any] = {
    "active_preset": "default",
    "presets": {
        "default": {
            "description": "Built-in fallback (models.yaml not found)",
            "models": {
                "openai": "gpt-5.4-mini",
                "gemini": "gemini-3.5-flash",
                "grok": "grok-4.3",
                "perplexity": "sonar",
                "tavily": "tavily-search",
                "claude": "claude-sonnet-4-6",
                "github": "gpt-5.4-mini",
                "glm": "glm-5.2",
                "summarizer": "gemini-3.5-flash",
                "site_summarizer": "gemini-3-flash-preview",
                "improver": "gpt-5.4-mini",
            },
            "reasoning_effort": "medium",
            "perplexity_search_type": "fast",
            "tavily_search_depth": "fast",
            "claude_max_budget": 5.0,
        }
    },
}

# Council defaults: members deliberate on topic improvement + keyword generation,
# with the boss (a strong reasoning model) synthesizing the final result. Members
# may use CLI-backed model specs ("codex:<m>", "agy:<m>") or plain API model ids.
DEFAULT_COUNCIL_MEMBERS = ["claude-opus-4-8", "codex:gpt-5.5", "agy:gemini-3.5-flash"]
DEFAULT_COUNCIL_BOSS = "claude-opus-4-8"
DEFAULT_BOOST_MAX_SUBPROJECTS = 5


def _default_config_path() -> Path:
    """Locate ``models.yaml`` independent of the current working directory.

    A ``models.yaml`` in the CWD wins (per-directory override), so callers can
    still drop a local config next to wherever they launch. Otherwise fall back
    to the ``models.yaml`` shipped at the repo/package root (one level above this
    module) — this is what makes the configured active preset the default for the
    CLI and UI no matter which directory they're started from.
    """
    cwd_path = Path(MODELS_FILENAME)
    if cwd_path.exists():
        return cwd_path
    return Path(__file__).resolve().parent.parent / MODELS_FILENAME


@dataclass
class EffectiveModels:
    """Resolved model versions for a run."""

    openai: str
    gemini: str
    grok: str
    perplexity: str
    tavily: str
    claude: str
    github: str
    glm: str
    summarizer: str
    site_summarizer: str  # Model for site research summarization
    improver: str  # Model for topic improvement and keyword generation
    reasoning_effort: str
    perplexity_search_type: str
    tavily_search_depth: str
    claude_max_budget: float
    preset_name: str
    preset_description: str = ""
    # LLM council: members deliberate on topic/keywords; boss synthesizes.
    council_members: list[str] = field(
        default_factory=lambda: list(DEFAULT_COUNCIL_MEMBERS)
    )
    council_boss: str = DEFAULT_COUNCIL_BOSS
    # Boost mode: when enabled and the council judges a topic worth decomposing,
    # the run fans out into up to ``boost_max_subprojects`` parallel sub-projects.
    boost_enabled: bool = False
    boost_max_subprojects: int = DEFAULT_BOOST_MAX_SUBPROJECTS
    # Plugin extensions (additive; empty for plugin-free configs so existing
    # fingerprints and result.json shapes stay byte-identical):
    # models.<name> keys that don't match a built-in slot land here, and the
    # preset's plugins: block provides per-extension option dicts.
    plugin_models: dict[str, str] = field(default_factory=dict)
    plugin_options: dict[str, dict[str, Any]] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Generate a short hash of the model configuration."""
        import json

        data = json.dumps(
            {
                "openai": self.openai,
                "gemini": self.gemini,
                "grok": self.grok,
                "perplexity": self.perplexity,
                "tavily": self.tavily,
                "claude": self.claude,
                "github": self.github,
                "glm": self.glm,
                "summarizer": self.summarizer,
                "site_summarizer": self.site_summarizer,
                "improver": self.improver,
                "reasoning_effort": self.reasoning_effort,
                "perplexity_search_type": self.perplexity_search_type,
                "tavily_search_depth": self.tavily_search_depth,
                "claude_max_budget": self.claude_max_budget,
                "council_members": self.council_members,
                "council_boss": self.council_boss,
                "boost_enabled": self.boost_enabled,
                "boost_max_subprojects": self.boost_max_subprojects,
                # only when non-empty: plugin-free fingerprints never change
                **({"plugin_models": self.plugin_models} if self.plugin_models else {}),
                **(
                    {"plugin_options": self.plugin_options}
                    if self.plugin_options
                    else {}
                ),
            },
            sort_keys=True,
        )
        return hashlib.sha256(data.encode()).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "openai": self.openai,
            "gemini": self.gemini,
            "grok": self.grok,
            "perplexity": self.perplexity,
            "tavily": self.tavily,
            "claude": self.claude,
            "github": self.github,
            "glm": self.glm,
            "summarizer": self.summarizer,
            "site_summarizer": self.site_summarizer,
            "improver": self.improver,
            "reasoning_effort": self.reasoning_effort,
            "perplexity_search_type": self.perplexity_search_type,
            "tavily_search_depth": self.tavily_search_depth,
            "claude_max_budget": self.claude_max_budget,
            "council_members": self.council_members,
            "council_boss": self.council_boss,
            "boost_enabled": self.boost_enabled,
            "boost_max_subprojects": self.boost_max_subprojects,
            **({"plugin_models": self.plugin_models} if self.plugin_models else {}),
            **({"plugin_options": self.plugin_options} if self.plugin_options else {}),
            "preset_name": self.preset_name,
            "preset_description": self.preset_description,
            "fingerprint": self.fingerprint(),
        }


class SystemConfigManager:
    """Manages system-level model configuration from models.yaml."""

    def __init__(self, config_path: Path | str | None = None) -> None:
        """
        Initialize the config manager.

        Args:
            config_path: Path to models.yaml. Defaults to ./models.yaml
        """
        if config_path is None:
            config_path = _default_config_path()
        self.config_path = Path(config_path)
        self._config: dict[str, Any] | None = None
        self._config_mtime: float | None = None

    @property
    def _active_preset_path(self) -> Path:
        return self.config_path.parent / ACTIVE_PRESET_STATE_FILENAME

    def load(self) -> dict[str, Any]:
        """Load configuration from models.yaml, reloading if the file changed.

        Caching keyed on mtime means a preset/model edit (from the UI or an
        external editor) is picked up on the next run instead of being frozen for
        the life of the process (review M1). A missing file yields the embedded
        default rather than crashing (review M16).
        """
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            if self._config is None:
                logger.warning(
                    "Config file not found (%s); using built-in defaults",
                    self.config_path,
                )
                self._config = dict(_EMBEDDED_DEFAULT_CONFIG.items())
                self._config_mtime = None
            return self._config

        if self._config is not None and self._config_mtime == mtime:
            return self._config

        with open(self.config_path) as f:
            self._config = yaml.safe_load(f) or {}
        self._config_mtime = mtime
        logger.debug(f"Loaded config from {self.config_path}")
        return self._config

    def save(self, config: dict[str, Any]) -> None:
        """Save the full configuration to models.yaml atomically."""
        atomic_write_text(
            self.config_path,
            yaml.dump(config, default_flow_style=False, sort_keys=False),
        )
        self._config = config
        try:
            self._config_mtime = self.config_path.stat().st_mtime
        except OSError:
            self._config_mtime = None
        logger.info(f"Saved config to {self.config_path}")

    def get_preset_names(self) -> list[str]:
        """Get list of available preset names."""
        config = self.load()
        return list(config.get("presets", {}).keys())

    def get_active_preset(self) -> str:
        """Get the currently active preset name.

        The runtime override lives in a small sidecar file (read fresh every
        call, so a UI change applies immediately); if absent, fall back to the
        ``active_preset`` declared in models.yaml. (Review L21, M1.)
        """
        override = self._read_active_preset_override()
        if override:
            return override
        return self.load().get("active_preset", "default")

    def _read_active_preset_override(self) -> str | None:
        try:
            data = json.loads(self._active_preset_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = data.get("active_preset") if isinstance(data, dict) else None
        return value if isinstance(value, str) and value else None

    def set_active_preset(self, preset_name: str) -> None:
        """Set the active preset by writing the sidecar (models.yaml untouched)."""
        config = self.load()
        if preset_name not in config.get("presets", {}):
            raise ValueError(f"Unknown preset: {preset_name}")
        atomic_write_text(
            self._active_preset_path,
            json.dumps({"active_preset": preset_name}, indent=2),
        )
        logger.info(f"Set active preset to {preset_name}")

    def resolve_effective_models(
        self, preset_name: str | None = None
    ) -> EffectiveModels:
        """Resolve the effective model versions for a run."""
        config = self.load()

        if preset_name is None:
            preset_name = self.get_active_preset()

        presets = config.get("presets", {})
        if preset_name not in presets:
            raise ValueError(f"Unknown preset: {preset_name}")

        preset = presets[preset_name]
        models = preset.get("models", {})

        # Council config (preset-level, falls back to module defaults).
        council = preset.get("council", {}) or {}
        council_members = council.get("members") or list(DEFAULT_COUNCIL_MEMBERS)
        council_boss = council.get("boss") or DEFAULT_COUNCIL_BOSS

        # Boost config (opt-in; the council still gates whether decomposition happens).
        boost = preset.get("boost", {}) or {}
        boost_enabled = bool(boost.get("enabled", False))
        boost_max_subprojects = int(
            boost.get("max_subprojects", DEFAULT_BOOST_MAX_SUBPROJECTS)
        )

        # Plugin extensions: models.<name> keys that aren't built-in slots
        # become plugin model assignments (a preset naming an uninstalled
        # plugin is a warning, never an error); the plugins: block carries
        # per-extension option dicts.
        builtin_slots = {
            "openai",
            "gemini",
            "grok",
            "perplexity",
            "tavily",
            "claude",
            "github",
            "glm",
            "summarizer",
            "site_summarizer",
            "improver",
        }
        plugin_models = {
            str(k): str(v) for k, v in models.items() if k not in builtin_slots
        }
        plugins_block = preset.get("plugins", {}) or {}
        plugin_options = {
            str(k): dict(v) for k, v in plugins_block.items() if isinstance(v, dict)
        }

        return EffectiveModels(
            openai=models["openai"],
            gemini=models["gemini"],
            grok=models["grok"],
            perplexity=models["perplexity"],
            tavily=models.get("tavily", "tavily-search"),
            claude=models.get("claude", "claude-opus-4-7"),
            github=models.get("github", "gpt-5.5"),
            glm=models.get("glm", "glm-4.6"),
            summarizer=models["summarizer"],
            site_summarizer=models.get("site_summarizer", "gemini-3-flash-preview"),
            improver=models.get("improver", "gpt-5.5"),
            reasoning_effort=preset.get("reasoning_effort", "medium"),
            perplexity_search_type=preset.get("perplexity_search_type", "pro"),
            tavily_search_depth=preset.get("tavily_search_depth", "advanced"),
            claude_max_budget=preset.get("claude_max_budget", 5.0),
            preset_name=preset_name,
            preset_description=preset.get("description", ""),
            council_members=list(council_members),
            council_boss=council_boss,
            boost_enabled=boost_enabled,
            boost_max_subprojects=boost_max_subprojects,
            plugin_models=plugin_models,
            plugin_options=plugin_options,
        )
