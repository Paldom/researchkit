"""
Build a Claude Code starter prompt for iterating on a project's final article.

The prompt is a self-contained block the user can paste into a Claude Code
session to load the entire research project context (config, raw results,
formatted report, user-curated sources, log) and then iterate on the article.
"""

from __future__ import annotations

from researchkit.project import (
    CONFIG_FILENAME,
    LOG_FILENAME,
    REPORT_FILENAME,
    RESULT_FILENAME,
    USER_SOURCES_DIRNAME,
    Project,
)


def build_article_prompt(project: Project) -> str:
    """
    Generate a starter prompt for Claude Code to load the project and help
    iterate on the article portion.

    Uses absolute paths so the prompt works regardless of which directory
    the Claude Code session is launched from.
    """
    abs_path = project.path.resolve()
    cfg = project.config

    providers = ", ".join(cfg.providers) if cfg.providers else "(none configured)"
    user_sources_line = ""
    if project.user_sources_dir.exists():
        n_urls = len(cfg.user_url_sources)
        n_files = len(cfg.user_file_sources)
        user_sources_line = (
            f"4. `{abs_path}/{USER_SOURCES_DIRNAME}/` — my own reference materials "
            f"({n_urls} URL(s), {n_files} document(s)). URLs are citable in the "
            f"article; documents are background context only — do not cite the "
            f"document files themselves, but cite URLs, books, papers, or named "
            f"works found inside them when used.\n"
        )
    else:
        user_sources_line = (
            f"4. `{abs_path}/{USER_SOURCES_DIRNAME}/` — (no user-curated sources "
            f"for this project)\n"
        )

    return (
        f"I'm iterating on the article for a research project. Please load the "
        f"full context.\n\n"
        f"**Project location:** `{abs_path}`\n"
        f"**Topic:** {cfg.topic}\n"
        f"**Time window:** last {cfg.days} days\n"
        f"**Providers used:** {providers}\n\n"
        f"Read the following files in this order:\n\n"
        f"1. `{abs_path}/{REPORT_FILENAME}` — the current formatted report. The "
        f"`## Professional Overview` section is the article draft I want to "
        f"iterate on. `## Digest` is the concise scannable version. "
        f"`## User-Curated Sources` lists URLs I've curated that must stay "
        f"cited.\n"
        f"2. `{abs_path}/{CONFIG_FILENAME}` — the research configuration "
        f"(topic, keywords, providers, lookback window, user sources).\n"
        f"3. `{abs_path}/{RESULT_FILENAME}` — raw provider outputs, individual "
        f"summaries, meta-summary, and all citations. Use this when I ask you "
        f"to add or swap citations, or pull in a finding I want to highlight.\n"
        f"{user_sources_line}"
        f"5. `{abs_path}/{LOG_FILENAME}` — execution log. Only consult if I ask "
        f"about errors, timing, or which providers failed.\n\n"
        f"After reading, give me a 2-3 sentence summary of the project (topic, "
        f"key findings from the meta-summary, and what I curated) and wait for "
        f"my instructions. Common things I'll ask for:\n\n"
        f"- Rewrite the article in a different tone or for a different audience\n"
        f"- Shorten or expand specific sections\n"
        f"- Reorganize the article around a different theme or thesis\n"
        f"- Add citations from `{RESULT_FILENAME}` or my user-curated sources\n"
        f"- Polish for publication (style, hedging, citation density, headlines)\n\n"
        f"Show diffs before writing to any file; do not modify "
        f"`{REPORT_FILENAME}` or `{RESULT_FILENAME}` until I confirm."
    )
