"""Tests for ExaConnector."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from researchkit.site_research.connectors.exa import ExaConnector
from researchkit.site_research.types import SiteItem, SiteItemSummary


class MockExaResult:
    """Mock Exa search result."""

    def __init__(
        self,
        url: str = "https://example.com/article",
        title: str = "Test Article",
        text: str = "This is the article content.",
        highlights: list[str] | None = None,
        highlight_scores: list[float] | None = None,
        author: str | None = "Test Author",
        published_date: str | None = "2024-01-15",
        summary: str | None = None,
        id: str = "exa-123",
    ):
        self.url = url
        self.title = title
        self.text = text
        self.highlights = highlights or ["Key insight 1", "Key insight 2"]
        self.highlight_scores = highlight_scores or [0.9, 0.8]
        self.author = author
        self.published_date = published_date
        self.summary = summary
        self.id = id


class MockExaResponse:
    """Mock Exa search response."""

    def __init__(
        self, results: list[MockExaResult] | None = None, context: str | None = None
    ):
        self.results = results or [MockExaResult()]
        self.context = context


class TestExaConnectorInit:
    """Tests for ExaConnector initialization."""

    def test_init_with_defaults(self) -> None:
        """Test initialization with default values."""
        connector = ExaConnector()
        assert connector.search_type == "deep"
        assert connector.num_results == 100
        assert connector.include_context is True
        assert connector.text_max_characters == 3000
        assert connector.highlights_per_url == 3

    def test_init_with_custom_values(self) -> None:
        """Test initialization with custom values."""
        connector = ExaConnector(
            api_key="test-key",
            search_type="neural",
            num_results=50,
            include_context=False,
            category="news",
        )
        assert connector.api_key == "test-key"
        assert connector.search_type == "neural"
        assert connector.num_results == 50
        assert connector.include_context is False
        assert connector.category == "news"

    def test_num_results_capped_at_100(self) -> None:
        """Test that num_results is capped at 100."""
        connector = ExaConnector(num_results=200)
        assert connector.num_results == 100


class TestExaConnectorAvailability:
    """Tests for availability checks."""

    def test_is_available_with_api_key(self) -> None:
        """Test is_available returns True when API key is set."""
        connector = ExaConnector(api_key="test-key")
        assert connector.is_available() is True

    def test_is_available_without_api_key(self) -> None:
        """Test is_available returns False when API key is not set."""
        connector = ExaConnector(api_key=None)
        # Clear any env var that might be set
        with patch.dict("os.environ", {}, clear=True):
            connector = ExaConnector()
            connector.api_key = None
            assert connector.is_available() is False

    def test_summarizer_is_available_with_gemini_key(self) -> None:
        """Test summarizer_is_available returns True when Gemini API key is set."""
        connector = ExaConnector(gemini_api_key="test-gemini-key")
        assert connector.summarizer_is_available() is True

    def test_summarizer_is_available_without_gemini_key(self) -> None:
        """Per-item summarize() is local (no Gemini call), so availability is
        always True regardless of the Gemini key. (Review S5.)"""
        connector = ExaConnector(gemini_api_key=None)
        connector.gemini_api_key = None
        assert connector.summarizer_is_available() is True


class TestExaConnectorSearch:
    """Tests for search functionality."""

    @patch("researchkit.site_research.connectors.exa.ExaConnector._get_exa_client")
    def test_search_returns_site_items(self, mock_get_client: MagicMock) -> None:
        """Test that search returns list of SiteItem objects."""
        mock_client = MagicMock()
        mock_client.search_and_contents.return_value = MockExaResponse(
            results=[
                MockExaResult(url="https://example.com/1", title="Article 1"),
                MockExaResult(url="https://example.com/2", title="Article 2"),
            ]
        )
        mock_get_client.return_value = mock_client

        connector = ExaConnector(api_key="test-key")
        published_after = datetime(2024, 1, 1, tzinfo=UTC)

        results = connector.search("AI research", published_after, 10)

        assert len(results) == 2
        assert all(isinstance(r, SiteItem) for r in results)
        assert results[0].site == "exa"
        assert results[0].url == "https://example.com/1"
        assert results[1].url == "https://example.com/2"

    @patch("researchkit.site_research.connectors.exa.ExaConnector._get_exa_client")
    def test_search_with_additional_queries(self, mock_get_client: MagicMock) -> None:
        """Test that additional_queries are passed to Exa."""
        mock_client = MagicMock()
        mock_client.search_and_contents.return_value = MockExaResponse()
        mock_get_client.return_value = mock_client

        connector = ExaConnector(api_key="test-key")
        published_after = datetime(2024, 1, 1, tzinfo=UTC)
        keywords = ["keyword1", "keyword2"]

        connector.search(
            "AI research", published_after, 10, additional_queries=keywords
        )

        # Verify additional_queries was passed
        call_kwargs = mock_client.search_and_contents.call_args[1]
        assert call_kwargs["additional_queries"] == keywords

    @patch("researchkit.site_research.connectors.exa.ExaConnector._get_exa_client")
    def test_search_with_deep_type(self, mock_get_client: MagicMock) -> None:
        """Test that deep search type is used."""
        mock_client = MagicMock()
        mock_client.search_and_contents.return_value = MockExaResponse()
        mock_get_client.return_value = mock_client

        connector = ExaConnector(api_key="test-key", search_type="deep")
        published_after = datetime(2024, 1, 1, tzinfo=UTC)

        connector.search("AI research", published_after, 10)

        call_kwargs = mock_client.search_and_contents.call_args[1]
        assert call_kwargs["type"] == "deep"

    @patch("researchkit.site_research.connectors.exa.ExaConnector._get_exa_client")
    def test_search_date_filtering(self, mock_get_client: MagicMock) -> None:
        """Test that date filtering is applied."""
        mock_client = MagicMock()
        mock_client.search_and_contents.return_value = MockExaResponse()
        mock_get_client.return_value = mock_client

        connector = ExaConnector(api_key="test-key")
        published_after = datetime(2024, 6, 15, tzinfo=UTC)

        connector.search("AI research", published_after, 10)

        call_kwargs = mock_client.search_and_contents.call_args[1]
        assert call_kwargs["start_published_date"] == "2024-06-15"

    @patch("researchkit.site_research.connectors.exa.ExaConnector._get_exa_client")
    def test_search_stores_content_in_popularity(
        self, mock_get_client: MagicMock
    ) -> None:
        """Test that text and highlights are stored in popularity dict."""
        mock_client = MagicMock()
        mock_client.search_and_contents.return_value = MockExaResponse(
            results=[
                MockExaResult(
                    text="Article content here",
                    highlights=["Highlight 1", "Highlight 2"],
                    highlight_scores=[0.95, 0.85],
                )
            ]
        )
        mock_get_client.return_value = mock_client

        connector = ExaConnector(api_key="test-key")
        published_after = datetime(2024, 1, 1, tzinfo=UTC)

        results = connector.search("AI research", published_after, 10)

        assert results[0].popularity["text"] == "Article content here"
        assert results[0].popularity["highlights"] == ["Highlight 1", "Highlight 2"]
        assert results[0].popularity["highlight_scores"] == [0.95, 0.85]

    def test_search_without_api_key_returns_empty(self) -> None:
        """Test that search returns empty list without API key."""
        connector = ExaConnector()
        connector.api_key = None
        published_after = datetime(2024, 1, 1, tzinfo=UTC)

        results = connector.search("AI research", published_after, 10)

        assert results == []

    @patch("researchkit.site_research.connectors.exa.ExaConnector._get_exa_client")
    def test_search_reraises_exception(self, mock_get_client: MagicMock) -> None:
        """search() now re-raises hard failures so the orchestrator records them
        in bundle.errors instead of silently returning []. (Review M12.)"""
        mock_get_client.side_effect = Exception("API error")

        connector = ExaConnector(api_key="test-key")
        published_after = datetime(2024, 1, 1, tzinfo=UTC)

        with pytest.raises(Exception, match="API error"):
            connector.search("AI research", published_after, 10)


class TestExaConnectorSummarize:
    """Tests for summarization functionality."""

    def test_summarize_without_gemini_key(self) -> None:
        """summarize() is local — it works without a Gemini key, returning the
        Exa-provided summary/highlights (or a placeholder). (Review S5.)"""
        connector = ExaConnector()
        connector.gemini_api_key = None

        item = SiteItem(
            site="exa",
            query="test",
            title="Test Article",
            url="https://example.com",
            popularity={"text": "Content", "highlights": ["Key point"]},
        )

        result = connector.summarize("AI research", item)

        assert isinstance(result, SiteItemSummary)
        assert result.tldr == ["Key point"]

    def test_summarize_without_content(self) -> None:
        """With no exa_summary and no highlights, summarize returns a placeholder."""
        connector = ExaConnector(gemini_api_key="test-key")

        item = SiteItem(
            site="exa",
            query="test",
            title="Test Article",
            url="https://example.com",
            popularity={"text": "", "highlights": []},
        )

        result = connector.summarize("AI research", item)

        assert isinstance(result, SiteItemSummary)
        assert "No summary available" in result.tldr[0]

    def test_summarize_fallback_to_exa_summary(self) -> None:
        """Test that summarize falls back to Exa summary when no text."""
        connector = ExaConnector(gemini_api_key="test-key")

        item = SiteItem(
            site="exa",
            query="test",
            title="Test Article",
            url="https://example.com",
            popularity={
                "text": "",
                "highlights": [],
                "exa_summary": "Exa generated summary",
            },
        )

        result = connector.summarize("AI research", item)

        assert result.tldr[0] == "Exa generated summary"

    def test_summarize_uses_exa_summary_and_highlights(self) -> None:
        """summarize() combines the Exa-provided summary with its highlights
        (no Gemini call). This replaces the old JSON-parsing test that exercised
        a Gemini path summarize() no longer uses. (Review S5.)"""
        connector = ExaConnector(gemini_api_key="test-key")
        item = SiteItem(
            site="exa",
            query="test",
            title="Test Article",
            url="https://example.com",
            popularity={
                "text": "Content here",
                "exa_summary": "The gist of the article",
                "highlights": ["Highlight A", "Highlight B"],
            },
        )

        result = connector.summarize("AI research", item)

        assert result.tldr[0] == "The gist of the article"
        assert "Highlight A" in result.tldr
        assert result.key_takeaways == ["Highlight A", "Highlight B"]


class TestExaConnectorPopularityScore:
    """Tests for popularity score calculation."""

    def test_popularity_score_with_highlight_scores(self) -> None:
        """Test popularity score calculation with highlight scores."""
        connector = ExaConnector()
        item = SiteItem(
            site="exa",
            query="test",
            title="Test",
            url="https://example.com",
            popularity={"highlight_scores": [0.9, 0.8, 0.7]},
        )

        score = connector.popularity_score(item)

        assert score == pytest.approx(0.8, rel=0.01)  # Average of 0.9, 0.8, 0.7

    def test_popularity_score_without_highlight_scores(self) -> None:
        """Test popularity score returns 0 without highlight scores."""
        connector = ExaConnector()
        item = SiteItem(
            site="exa",
            query="test",
            title="Test",
            url="https://example.com",
            popularity={},
        )

        score = connector.popularity_score(item)

        assert score == 0.0


class TestExaConnectorParseResponse:
    """Tests for response parsing."""

    def test_parse_extraction_response_valid_json(self) -> None:
        """Test parsing valid JSON response."""
        connector = ExaConnector()
        text = '{"tldr": ["Point 1"], "key_takeaways": ["Takeaway 1"]}'

        result = connector._parse_extraction_response(text)

        assert result.tldr == ["Point 1"]
        assert result.key_takeaways == ["Takeaway 1"]

    def test_parse_extraction_response_json_in_markdown(self) -> None:
        """Test parsing JSON embedded in markdown code block."""
        connector = ExaConnector()
        text = '```json\n{"tldr": ["Point 1"]}\n```'

        result = connector._parse_extraction_response(text)

        assert result.tldr == ["Point 1"]

    def test_parse_extraction_response_invalid_json(self) -> None:
        """Test parsing invalid JSON falls back gracefully."""
        connector = ExaConnector()
        text = "This is not JSON at all"

        result = connector._parse_extraction_response(text)

        assert "This is not JSON" in result.tldr[0]
        assert result.summarization_error == "JSON parse failed"
