"""Regression test for M2: Perplexity recency/domain filters must be sent as
TOP-LEVEL create() params, not nested inside web_search_options (where the API
silently ignores them)."""

from __future__ import annotations

from unittest.mock import MagicMock

from researchkit.providers.perplexity_provider import PerplexityProvider


def _fake_response() -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="text"))]
    resp.search_results = []
    resp.usage = None
    return resp


def test_recency_and_domain_filters_are_top_level() -> None:
    provider = PerplexityProvider(
        api_key="test", search_type="fast"
    )  # fast -> non-stream
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response()

    provider._run_query(
        client,
        system_prompt="sys",
        user_prompt="user",
        recency_filter="week",
        domain_filter=["reddit.com", "x.com"],
    )

    kwargs = client.chat.completions.create.call_args.kwargs
    # Top-level params (the fix)
    assert kwargs.get("search_recency_filter") == "week"
    assert kwargs.get("search_domain_filter") == ["reddit.com", "x.com"]
    # web_search_options must NOT carry them anymore
    wso = kwargs.get("web_search_options", {})
    assert "search_recency_filter" not in wso
    assert "search_domain_filter" not in wso
    assert wso.get("search_context_size") == "high"


def test_no_domain_filter_omits_the_kwarg() -> None:
    provider = PerplexityProvider(api_key="test", search_type="fast")
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response()

    provider._run_query(
        client,
        system_prompt="s",
        user_prompt="u",
        recency_filter="day",
        domain_filter=None,
    )

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs.get("search_recency_filter") == "day"
    assert "search_domain_filter" not in kwargs
