"""Link analytics over a project folder."""

from __future__ import annotations

from pathlib import Path


def test_boosted_parent_folds_in_its_subprojects_links(tmp_path: Path) -> None:
    """A boosted parent's result.json has no citations of its own."""
    import json

    from researchkit.link_analytics import analyze_project_links, load_link_result

    (tmp_path / "result.json").write_text(json.dumps({"decomposed": True}))
    for i, url in enumerate(["https://a.io/x", "https://b.io/y"]):
        sub = tmp_path / "subprojects" / f"sub_{i}"
        sub.mkdir(parents=True)
        sub.joinpath("result.json").write_text(
            json.dumps(
                {
                    "provider_results": [
                        {"provider": "grok", "sources": [{"url": url}]}
                    ],
                    "site_research": {"items_by_site": {"exa": [{"url": url}]}},
                }
            )
        )
    merged = load_link_result(tmp_path)
    assert merged is not None
    out = analyze_project_links(merged)
    assert out["citations"]["summary"]["unique_urls"] == 2
    assert out["site_research"]["summary"]["unique_urls"] == 2
    assert load_link_result(tmp_path / "missing") is None
