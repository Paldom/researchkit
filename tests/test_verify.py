"""PLAN 4.3: claim labels from the archive; unavailable never counts as support."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from researchkit import verify


def _row(
    i: int, sentence: str, status: str = "fetched", file: str | None = "a.md"
) -> dict:
    return {
        "id": f"sha256:{i}",
        "sentence": sentence,
        "canonical_url": "https://a.io/x",
        "archive_status": status,
        "material_file": file,
    }


def test_labels_supported_contradicted_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import researchkit.publish as publish_module

    (tmp_path / "materials").mkdir()
    (tmp_path / "materials" / "a.md").write_text(
        "---\ntitle: A\n---\nThe tool caches searches for 24 hours.", encoding="utf-8"
    )
    rows = [
        _row(1, "Searches are cached for 24 hours."),
        _row(2, "Searches are never cached."),
        _row(3, "A walled claim.", status="tos-blocked", file=None),
        _row(4, "The judge forgot this one."),
    ]
    (tmp_path / "claims.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
    )
    prompts: list[str] = []

    def fake_write(self: Any, prompt: str) -> str:
        prompts.append(prompt)
        keys = re.findall(r"- (c\d+): ", prompt)
        assert len(keys) == 3  # the unarchived claim never reaches the judge
        assert "title: A" not in prompt  # frontmatter stripped
        return json.dumps(
            {
                keys[0]: {"label": "supported", "why": "caches searches for 24 hours"},
                keys[1]: {"label": "contradicted", "why": "it does cache"},
            }
        )

    monkeypatch.setattr(publish_module._Writer, "write", fake_write)
    counts = verify.verify_claims(tmp_path, model="m", max_budget=1.0)

    assert len(prompts) == 1
    assert counts == {
        "model": "m",
        "total": 4,
        "supported": 1,
        "unsupported": 0,
        "contradicted": 1,
        "unavailable": 2,
    }
    labels = [
        json.loads(line)
        for line in (tmp_path / "claims.labels.jsonl").read_text().splitlines()
    ]
    assert [x["label"] for x in labels] == [
        "supported",
        "contradicted",
        "unavailable",
        "unavailable",
    ]
    # Labels sit beside the claims; the claims file itself is untouched.
    assert (tmp_path / "claims.jsonl").read_text().count("sha256:") == 4


def test_sources_are_packed_into_bounded_calls() -> None:
    texts = {f"s{i}.md": "w " * 8000 for i in range(3)}
    groups: dict[str, list[dict[str, Any]]] = {name: [] for name in texts}
    assert [len(b) for b in verify._batches(groups, texts)] == [1, 1, 1]
    small = {f"s{i}.md": "w " * 100 for i in range(5)}
    assert verify._batches({n: [] for n in small}, small) == [list(small)]
