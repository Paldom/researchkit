"""researchkit: define a topic, research everywhere with an LLM council."""

import logging
import os
import re
import sys
from importlib.metadata import version
from logging import NullHandler

__version__ = version("researchkit")

# gRPC fork-safety. This package runs gRPC-backed providers (google-genai,
# xai-sdk) concurrently with CLI subprocesses (claude/codex/agy). Those
# subprocesses launch with start_new_session=True to kill the whole process
# group on timeout, which on CPython 3.12+ forces fork()+exec() instead of
# posix_spawn. fork() runs the C runtime's pthread_atfork handlers (glibc on
# Linux, libSystem on macOS); gRPC registers one, and when a c-ares DNS resolve
# is in flight it hits `CHECK(channel_ != nullptr)` and aborts the forked child
# (SIGABRT/SIGSEGV) before it can exec the CLI — surfacing as "Claude Code CLI
# exited with code -6/-11".
#
# We only ever fork+exec (no multiprocessing/os.fork, no preexec_fn, no
# fork-and-keep-using-gRPC-in-child), so gRPC's fork handlers aren't needed and
# are in fact incompatible with our pattern. FORCE it off (not setdefault): an
# inherited GRPC_ENABLE_FORK_SUPPORT=1 from a shell/container would otherwise
# silently re-enable the crashing atfork path. Must run before any import pulls
# in grpc (gemini_provider imports google.genai at module load) — warn if we're
# already too late so an import-order regression is visible.
if "grpc._cython.cygrpc" in sys.modules:
    logging.getLogger(__name__).warning(
        "grpc was imported before the researchkit package init; "
        "GRPC_ENABLE_FORK_SUPPORT=0 may not take effect and fork()+exec() "
        "with live gRPC can crash."
    )
os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "0"
# Quiet gRPC's INFO-level fork/resolver chatter by default; still shows errors,
# and an operator can override it. (Not part of the correctness fix.)
os.environ.setdefault("GRPC_VERBOSITY", "error")

# Add NullHandler to avoid "No handler found" warnings when used as library
logging.getLogger(__name__).addHandler(NullHandler())


def slugify_topic(topic: str) -> str:
    """Normalize a research topic into a filesystem-safe ASCII slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    if not slug:
        raise ValueError("topic must contain at least one ASCII alphanumeric character")
    return slug


from researchkit.project import (  # noqa: E402
    Project,
    ProjectConfig,
    create_project,
    list_projects,
    load_project,
)
from researchkit.service import (  # noqa: E402
    ResearchArtifacts,
    ResearchRequest,
    SocialResearchService,
)

__all__ = [
    # Project management
    "Project",
    "ProjectConfig",
    "ResearchArtifacts",
    # Service layer
    "ResearchRequest",
    "SocialResearchService",
    "__version__",
    "create_project",
    "list_projects",
    "load_project",
    "slugify_topic",
]
