"""Agent profiles that bound what a harness CLI may do.

Kimi Code's headless ``-p`` mode auto-approves every tool, including its
shell: a planted instruction in a fetched page ran ``touch`` on the host
(verified 2026-10-21). An ``--agent-file`` allowlist is the CLI's own
containment. A resumed session keeps the profile it started with, also
verified.
"""

from __future__ import annotations

from importlib.resources import files


def profile_path(name: str) -> str:
    """Filesystem path of a bundled profile (``kimi-researcher.md`` …)."""
    return str(files("researchkit.harness_profiles").joinpath(name))
