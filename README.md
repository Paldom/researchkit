# researchkit

Define topic, research everywhere with LLM council: parallel AI web search (OpenAI, Gemini, Grok, Perplexity) synthesized into one cited report

## Usage

```python
from researchkit import slugify_topic

slugify_topic("  LLM Council: Research!  ")  # "llm-council-research"
```

## Development

```bash
uv sync
uv run pre-commit install
# full quality gate:
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest --cov -q
```
