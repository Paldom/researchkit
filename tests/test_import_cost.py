"""Startup must not load the API SDKs: every CLI call, MCP boot and web
request pays for whatever `import researchkit.cli` drags in."""

from __future__ import annotations

import subprocess
import sys


def test_cli_import_skips_api_sdks() -> None:
    code = (
        "import sys, researchkit.cli, researchkit.providers.base;"
        "heavy = [m for m in ('google.genai', 'openai', 'xai_sdk', 'perplexity')"
        " if m in sys.modules];"
        "print(','.join(heavy))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_lazy_exports_still_resolve() -> None:
    import researchkit
    import researchkit.providers as providers

    assert providers.ClaudeProvider.__name__ == "ClaudeProvider"
    assert researchkit.SocialResearchService.__name__ == "SocialResearchService"
    assert "GeminiProvider" in dir(providers)
