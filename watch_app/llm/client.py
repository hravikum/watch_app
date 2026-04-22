"""Thin wrapper around the Anthropic SDK.

Deliberately minimal — no framework, no abstractions beyond what we actually
need. Provides:
- A lazily-constructed client so importing this module is free.
- `extract_makers()` — the one LLM-backed operation Phase 0 uses.
- Transparent caching: repeat calls with the same input hit the DB cache
  instead of the API.

Design choice: the cache is consulted inside this module rather than in the
caller. That way every LLM entry point gets caching for free and we can't
forget to apply it.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from watch_app.config import get_settings
from watch_app.llm.cache import (
    _normalize_for_hashing,
    get_cached,
    set_cached,
)
from watch_app.llm.prompts import (
    EXTRACT_MAKERS_PROMPT_VERSION,
    format_extract_prompt,
)
from watch_app.llm.schemas import ExtractionResult

if TYPE_CHECKING:
    from anthropic import Anthropic


logger = logging.getLogger(__name__)

_client: "Anthropic | None" = None


def _get_client() -> "Anthropic":
    """Lazy client construction. Fails loud if no API key is configured."""
    global _client
    if _client is None:
        from anthropic import Anthropic  # import here to keep module import cheap

        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Either set it in .env or run "
                "without --use-llm."
            )
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


def extract_makers(
    session: Session,
    title: str,
    text: str,
) -> ExtractionResult:
    """Ask Claude to extract watchmaker mentions from article text.

    Results are cached by hash of (model, prompt_version, title+text), so the
    same article is only processed once even across multiple scan runs.
    """
    settings = get_settings()
    model = settings.llm_model

    # Input content for cache key — deterministic.
    input_content = _normalize_for_hashing({"title": title, "text": text})

    # Try cache first.
    cached = get_cached(
        session=session,
        model=model,
        prompt_version=EXTRACT_MAKERS_PROMPT_VERSION,
        input_content=input_content,
    )
    if cached is not None:
        logger.debug("LLM cache hit for title=%r", title)
        return ExtractionResult.model_validate(cached)

    # Cache miss — call the API.
    logger.info("LLM cache miss — calling Anthropic for title=%r", title)
    system_prompt, user_prompt = format_extract_prompt(title, text)

    client = _get_client()
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Pull the text out of the response. Anthropic returns a list of content
    # blocks; we expect a single text block containing JSON.
    raw_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            raw_text += block.text

    # Parse the JSON. Be lenient about leading/trailing whitespace or markdown
    # fences in case the model ignored our "no prose" instruction.
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        # Strip ``` or ```json fences.
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON response: %r", raw_text[:200])
        parsed = {"makers": []}

    result = ExtractionResult.model_validate(parsed)

    # Cache the validated result.
    set_cached(
        session=session,
        model=model,
        prompt_version=EXTRACT_MAKERS_PROMPT_VERSION,
        input_content=input_content,
        response=result.model_dump(),
    )

    return result
