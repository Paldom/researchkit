"""Prompt improver for refining topics and generating search keywords."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from researchkit.system_config import SystemConfigManager

logger = logging.getLogger(__name__)


class PromptImprover:
    """
    Improves topic text and generates search keywords.

    Supports multiple providers: OpenAI, Gemini, Grok, Perplexity.
    By default, uses the improver model from system config (gpt-5.2).
    """

    PROVIDERS: ClassVar[set[str]] = {
        "openai",
        "gemini",
        "grok",
        "perplexity",
        "glm",
        "kimi",
    }
    DEFAULT_PROVIDER = "openai"
    DEFAULT_MODEL = "gpt-5.4-mini"  # Default if no system config

    # Default models per provider — only a fallback when system config fails to
    # load; kept roughly in sync with models.yaml so a fallback isn't a dead id.
    DEFAULT_MODELS: ClassVar[dict[str, str]] = {
        "openai": "gpt-5.4-mini",
        "gemini": "gemini-3.5-flash",
        "grok": "grok-4.3",
        "perplexity": "sonar",
        "glm": "glm-5.2",
        "kimi": "kimi-k2.6",
    }

    def __init__(
        self,
        provider: str = "openai",
        model: str | None = None,
    ) -> None:
        """
        Initialize the prompt improver.

        Args:
            provider: Provider to use (openai, gemini, grok, perplexity)
            model: Model override (defaults to provider-specific default)
        """
        from researchkit.council import is_cli_backed_spec

        self.provider = provider.lower()
        # A CLI-backed model spec (codex:/agy:/grokcli:/claude:) overrides the
        # provider: the improver runs on the logged-in harness, no API key.
        if is_cli_backed_spec(model):
            self.provider = "cli"
        if self.provider not in self.PROVIDERS and self.provider != "cli":
            raise ValueError(
                f"Invalid provider: {provider}. Must be one of {self.PROVIDERS}"
            )

        self.model = model or self.DEFAULT_MODELS.get(self.provider)

    @classmethod
    def from_system_config(
        cls,
        config_manager: SystemConfigManager | None = None,
    ) -> PromptImprover:
        """
        Create a PromptImprover using the system config.

        Uses the improver model from the active preset (defaults to gpt-5.2).

        Args:
            config_manager: Optional SystemConfigManager. If None, creates a new one.

        Returns:
            PromptImprover configured with system config model
        """
        from researchkit.providers.glm_provider import is_glm_model
        from researchkit.providers.kimi_provider import is_kimi_model

        model = cls.DEFAULT_MODEL
        try:
            if config_manager is None:
                from researchkit.system_config import SystemConfigManager

                config_manager = SystemConfigManager()
            effective = config_manager.resolve_effective_models()
            model = effective.improver
        except Exception as e:
            logger.warning(f"Failed to load system config, using default model: {e}")

        from researchkit.council import is_cli_backed_spec

        # Route to the matching backend: harness spec -> CLI, GLM -> z.ai,
        # Kimi -> Moonshot, anything else -> OpenAI.
        if is_cli_backed_spec(model):
            provider = "cli"
        elif is_glm_model(model):
            provider = "glm"
        elif is_kimi_model(model):
            provider = "kimi"
        else:
            provider = "openai"
        return cls(provider=provider, model=model)

    def _get_openai_client(self) -> Any:
        """Get OpenAI client."""
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        return OpenAI(api_key=api_key)

    def _get_gemini_client(self) -> Any:
        """Get Gemini client."""
        from google import genai

        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY not set")
        return genai.Client(api_key=api_key)

    def _get_grok_client(self) -> Any:
        """Get Grok client (OpenAI-compatible)."""
        from openai import OpenAI

        api_key = os.getenv("XAI_API_KEY")
        if not api_key:
            raise RuntimeError("XAI_API_KEY not set")
        return OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")

    def _get_perplexity_client(self) -> Any:
        """Get Perplexity client."""
        from perplexity import Perplexity

        api_key = os.getenv("PERPLEXITY_API_KEY")
        if api_key:
            return Perplexity(api_key=api_key)
        return Perplexity()

    def _get_glm_client(self) -> Any:
        """Get GLM client (OpenAI-compatible, z.ai endpoint)."""
        from researchkit.providers.glm_provider import make_zai_client

        return make_zai_client()

    def _get_kimi_client(self) -> Any:
        """Get Kimi client (OpenAI-compatible, Moonshot endpoint)."""
        from researchkit.providers.kimi_provider import make_kimi_client

        return make_kimi_client()

    def _parse_json_response(self, text: str, key: str) -> Any:
        """Parse JSON response and extract a key (tolerant of fences/prose).

        The old fallback regex ``\\{[^{}]*\\}`` matched only a brace group with NO
        nested braces, so it could never recover the ``{"analysis": {...},
        "keywords": [...]}`` shape generate_keywords asks for — any fenced/prose-
        wrapped response silently yielded []. Scan for the first COMPLETE JSON
        object instead (handles nesting). (Review S11.)
        """
        text = text.strip()

        # Try direct JSON parse first.
        try:
            data = json.loads(text)
            if isinstance(data, dict) and key in data:
                return data[key]
        except json.JSONDecodeError:
            pass

        # Scan for the first complete JSON object containing the key.
        decoder = json.JSONDecoder()
        for start, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and key in obj:
                return obj[key]

        # Fallback: return original text for improve_topic, empty list for keywords
        logger.warning(f"Failed to parse JSON response: {text[:200]}")
        return text if key == "improved_topic" else []

    def _call_openai(self, system_prompt: str, user_prompt: str) -> str:
        """Call OpenAI API."""
        client = self._get_openai_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""

    def _call_gemini(self, system_prompt: str, user_prompt: str) -> str:
        """Call Gemini API."""
        from google.genai import types

        client = self._get_gemini_client()
        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
        response = client.models.generate_content(
            model=self.model,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                response_mime_type="application/json",
            ),
        )
        return response.text or ""

    def _call_grok(self, system_prompt: str, user_prompt: str) -> str:
        """Call Grok API (OpenAI-compatible)."""
        client = self._get_grok_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""

    def _call_perplexity(self, system_prompt: str, user_prompt: str) -> str:
        """Call Perplexity API."""
        client = self._get_perplexity_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
        )
        return response.choices[0].message.content or ""

    def _call_glm(self, system_prompt: str, user_prompt: str) -> str:
        """Call GLM API (OpenAI-compatible, z.ai endpoint)."""
        client = self._get_glm_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""

    def _call_kimi(self, system_prompt: str, user_prompt: str) -> str:
        """Call Kimi API (OpenAI-compatible, Moonshot endpoint).

        No temperature: current Kimi models run with fixed sampling params
        and reject overrides.
        """
        from researchkit.providers.kimi_provider import resolve_kimi_model

        client = self._get_kimi_client()
        response = client.chat.completions.create(
            model=resolve_kimi_model(self.model or ""),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""

    def _call_provider(self, system_prompt: str, user_prompt: str) -> str:
        """Call the configured provider."""
        if self.provider == "cli":
            from researchkit.council import complete_via_spec

            return complete_via_spec(
                self.model or "codex",
                system_prompt,
                user_prompt,
                label="improver.cli",
            )
        if self.provider == "openai":
            return self._call_openai(system_prompt, user_prompt)
        elif self.provider == "gemini":
            return self._call_gemini(system_prompt, user_prompt)
        elif self.provider == "grok":
            return self._call_grok(system_prompt, user_prompt)
        elif self.provider == "perplexity":
            return self._call_perplexity(system_prompt, user_prompt)
        elif self.provider == "glm":
            return self._call_glm(system_prompt, user_prompt)
        elif self.provider == "kimi":
            return self._call_kimi(system_prompt, user_prompt)
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

    def improve_topic(self, topic: str) -> str:
        """
        Refine topic text for better research results.

        Keeps 1-2 sentences, removes research instructions,
        focuses on the subject matter.

        Args:
            topic: The original topic text

        Returns:
            Improved topic text
        """
        if not topic.strip():
            return topic

        system_prompt = """You are a research topic refiner. Your job is to clean up research topics.
Return your response as valid JSON with the format: {"improved_topic": "..."}"""

        user_prompt = f"""Clean up the following research topic:
- Keep it to 1-2 concise sentences
- Focus on the subject matter, not research instructions
- Remove any guidance about how to research (e.g., "find articles about", "search for")
- Remove meta-instructions (e.g., "analyze", "compare", "summarize")
- Preserve the core intent and specific details
- Keep specific product names, technologies, or entities
- If the topic is already clean, return it as-is

Topic: {topic}

Return JSON: {{"improved_topic": "..."}}"""

        try:
            response = self._call_provider(system_prompt, user_prompt)
            result = self._parse_json_response(response, "improved_topic")
            if isinstance(result, str) and result.strip():
                return result.strip()
            return topic
        except Exception as e:
            logger.error(f"Failed to improve topic: {e}")
            return topic

    def generate_keywords(self, topic: str, count: int = 10) -> list[str]:
        """
        Generate high-quality, topic-focused search queries.

        Uses research-backed prompt engineering to generate specific, relevant
        keywords that avoid generic terms and focus on the research scope.

        Args:
            topic: The topic to generate keywords for
            count: Number of keywords to generate (default: 10)

        Returns:
            List of search query strings
        """
        if not topic.strip():
            return []

        system_prompt = """You are a research keyword specialist. Your task is to generate highly specific, search-effective queries that will find relevant content about a research topic.

CRITICAL RULES:
1. NEVER generate generic keywords that could apply to any topic
2. EVERY keyword MUST contain specific terms from the research topic
3. Focus on long-tail queries (3-6 words) that express clear search intent
4. Avoid single-word keywords or overly broad terms

Return your response as valid JSON: {"analysis": {...}, "keywords": [...]}"""

        user_prompt = f"""Generate {count} highly specific search queries for this research topic.

RESEARCH TOPIC: {topic}

STEP 1 - ANALYZE THE TOPIC:
First, identify:
- Core subject: What specific thing is being researched?
- Key entities: Names, products, technologies, companies mentioned
- Research scope: What aspect is the focus? (trends, opinions, tutorials, comparisons, problems)
- Constraints: Any time periods, platforms, or contexts specified?

STEP 2 - GENERATE KEYWORDS BY INTENT:
Create keywords across these search intent categories:

A) INFORMATIONAL (what/why/how it works):
   - "how [specific technology] works"
   - "[entity] explained"
   - "[topic] architecture overview"

B) TUTORIAL/PRACTICAL (how to do):
   - "[specific task] tutorial"
   - "[entity] implementation guide"
   - "building [specific thing] with [technology]"

C) DISCUSSION/OPINION (what people think):
   - "[entity] community feedback"
   - "[technology] real world experience"
   - "[product] honest review"

D) COMPARISON (vs alternatives):
   - "[entity] vs [likely alternative]"
   - "[technology] comparison"
   - "best [category] alternatives"

E) PROBLEM/SOLUTION (issues and fixes):
   - "[entity] common issues"
   - "[technology] troubleshooting"
   - "[specific problem] solution"

EXAMPLES OF GOOD vs BAD KEYWORDS:

Topic: "Claude AI coding assistant adoption in enterprise"
❌ BAD (too generic): "AI tools", "coding assistant", "enterprise software", "productivity"
✅ GOOD (specific): "Claude AI enterprise deployment", "Claude vs GitHub Copilot coding", "Claude API integration tutorial", "Claude AI developer experience review", "Anthropic Claude enterprise pricing"

Topic: "React Server Components performance impact"
❌ BAD (too generic): "React performance", "server components", "web development", "JavaScript"
✅ GOOD (specific): "React Server Components benchmarks", "RSC vs client components performance", "Next.js App Router RSC migration", "React Server Components real world results", "RSC streaming SSR comparison"

KEYWORD QUALITY CHECKLIST:
- [ ] Contains at least one specific entity/term from the topic
- [ ] Would return focused results, not millions of generic pages
- [ ] Matches content that would actually exist on the researched sites
- [ ] Expresses clear search intent (learn, compare, solve, discuss)
- [ ] 3-6 words, natural search phrasing

Return JSON:
{{
  "analysis": {{
    "core_subject": "...",
    "key_entities": ["...", "..."],
    "research_scope": "...",
    "likely_alternatives": ["...", "..."]
  }},
  "keywords": ["query1", "query2", ...]
}}"""

        try:
            response = self._call_provider(system_prompt, user_prompt)
            result = self._parse_json_response(response, "keywords")
            if isinstance(result, list):
                # Filter and clean keywords
                keywords = [str(k).strip() for k in result if k and str(k).strip()]
                # Remove any that are too short (likely generic)
                keywords = [k for k in keywords if len(k.split()) >= 2]
                return keywords[:count]
            return []
        except Exception as e:
            logger.error(f"Failed to generate keywords: {e}")
            return []
