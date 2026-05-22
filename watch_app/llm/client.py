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


# Model used for maker enrichment. Cheap, fast, sufficient for "what's the
# official website of <known watchmaker>". Hardcoded rather than read from
# settings because this is the only call site that needs it; if we ever
# add more enrichment use cases, factor out then.
ENRICHMENT_MODEL = "claude-haiku-4-5-20251001"

LOOKUP_WEBSITE_PROMPT_VERSION = "v1-2026-05-13"


def lookup_maker_website(
    session: Session,
    maker_name: str,
    country: str | None = None,
) -> str | None:
    """Ask Claude to identify the official website for a watchmaker.

    Returns a URL string, or None if the model is uncertain. The caller is
    expected to verify the URL resolves before committing it to the database
    — this function does not perform that check.

    Results are cached by (model, prompt_version, maker_name, country). The
    same maker is only looked up once even if enrich is run multiple times.

    Important: the prompt explicitly allows the model to reply NONE rather
    than guess. We prefer no data over wrong data.
    """
    # Cache key includes country since "SpaceOne France" might disambiguate
    # from a same-named entity elsewhere.
    input_content = _normalize_for_hashing(
        {"maker": maker_name, "country": country or ""}
    )

    cached = get_cached(
        session=session,
        model=ENRICHMENT_MODEL,
        prompt_version=LOOKUP_WEBSITE_PROMPT_VERSION,
        input_content=input_content,
    )
    if cached is not None:
        logger.debug("Website lookup cache hit for %r", maker_name)
        return cached.get("url")

    logger.info("Looking up website for %r via LLM", maker_name)

    country_hint = f" (based in {country})" if country else ""
    user_prompt = (
        f"What is the official website URL for the independent watchmaker "
        f"or watch brand named {maker_name!r}{country_hint}?\n\n"
        f"Rules:\n"
        f"- Reply with only the URL, no other text.\n"
        f"- The URL must be the brand's own website, not a dealer, "
        f"  aggregator, fan site, or social media.\n"
        f"- If you are not confident this is the correct watchmaker, or "
        f"  if you don't know their website, reply with exactly: NONE\n"
        f"- Prefer https:// over http://.\n"
    )

    client = _get_client()
    message = client.messages.create(
        model=ENRICHMENT_MODEL,
        max_tokens=100,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            raw_text += block.text

    cleaned = raw_text.strip()
    url: str | None
    if cleaned == "NONE" or not cleaned:
        url = None
    elif cleaned.startswith("http://") or cleaned.startswith("https://"):
        # Take the first whitespace-delimited token, in case the model
        # added a trailing period or explanation despite our instruction.
        url = cleaned.split()[0].rstrip(".,;)")
    else:
        # Anything that doesn't start with a URL scheme is suspicious.
        # Treat as NONE rather than try to coerce.
        logger.warning(
            "Unexpected LLM response for %r: %r — treating as NONE",
            maker_name, cleaned[:100],
        )
        url = None

    set_cached(
        session=session,
        model=ENRICHMENT_MODEL,
        prompt_version=LOOKUP_WEBSITE_PROMPT_VERSION,
        input_content=input_content,
        response={"url": url},
    )

    return url


LOOKUP_COUNTRY_PROMPT_VERSION = "v1-2026-05-20"
LOOKUP_INDEPENDENCE_PROMPT_VERSION = "v1-2026-05-20"

#: Countries Haiku is allowed to return for the country lookup. Anything
#: outside this list gets rejected even if the model is confident — it's the
#: structural verification mechanism (the equivalent of HTTP-resolve for URLs).
#: List skewed toward watchmaking countries. Not exhaustive geographically;
#: a maker from a country not on this list will get NONE, which is the
#: correct conservative behavior.
KNOWN_WATCHMAKING_COUNTRIES = frozenset(
    [
        "Switzerland",
        "Germany",
        "France",
        "Japan",
        "United Kingdom",
        "United States",
        "Italy",
        "Sweden",
        "Denmark",
        "Norway",
        "Finland",
        "Netherlands",
        "Belgium",
        "Austria",
        "Spain",
        "Portugal",
        "Czech Republic",
        "Poland",
        "Russia",
        "Ukraine",
        "China",
        "Hong Kong",
        "Taiwan",
        "South Korea",
        "Singapore",
        "Australia",
        "New Zealand",
        "Canada",
        "Mexico",
        "Brazil",
        "Argentina",
        "Chile",
        "India",
        "Israel",
        "Turkey",
        "South Africa",
        "Greece",
        "Ireland",
        "Hungary",
        "Romania",
        "Bulgaria",
        "Latvia",
        "Estonia",
        "Lithuania",
    ]
)


def lookup_maker_country(session: Session, maker_name: str) -> str | None:
    """Ask Claude for the country a watchmaker is based in.

    Returns a country string from KNOWN_WATCHMAKING_COUNTRIES, or None
    if the model is uncertain or returns a country outside our known
    list. The known-list check is the structural verification mechanism:
    we'd rather have None than a free-form string that doesn't match
    any country we recognize.

    Results are cached by (model, prompt_version, maker_name).
    """
    input_content = _normalize_for_hashing({"maker": maker_name})

    cached = get_cached(
        session=session,
        model=ENRICHMENT_MODEL,
        prompt_version=LOOKUP_COUNTRY_PROMPT_VERSION,
        input_content=input_content,
    )
    if cached is not None:
        logger.debug("Country lookup cache hit for %r", maker_name)
        return cached.get("country")

    logger.info("Looking up country for %r via LLM", maker_name)

    # Build the country list as a hint. We send it in the prompt so the
    # model knows the exact strings we accept — reduces "Czech Republic"
    # vs "Czechia" ambiguity.
    country_list = ", ".join(sorted(KNOWN_WATCHMAKING_COUNTRIES))
    user_prompt = (
        f"What country is the watchmaker or watch brand {maker_name!r} "
        f"primarily based in?\n\n"
        f"Rules:\n"
        f"- Reply with exactly one country name from this list, with no "
        f"other text: {country_list}\n"
        f"- If the maker is based in a country not on the list, or if you "
        f"are not confident, reply with exactly: NONE\n"
        f"- Match the country names exactly as written above (e.g., "
        f"'United Kingdom' not 'UK', 'United States' not 'USA').\n"
    )

    client = _get_client()
    message = client.messages.create(
        model=ENRICHMENT_MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            raw_text += block.text

    cleaned = raw_text.strip().rstrip(".,;)")
    country: str | None
    if cleaned == "NONE" or not cleaned:
        country = None
    elif cleaned in KNOWN_WATCHMAKING_COUNTRIES:
        country = cleaned
    else:
        # Model returned something outside the known list — treat as None.
        logger.info(
            "Country lookup for %r returned out-of-list value %r — treating as NONE",
            maker_name, cleaned[:50],
        )
        country = None

    set_cached(
        session=session,
        model=ENRICHMENT_MODEL,
        prompt_version=LOOKUP_COUNTRY_PROMPT_VERSION,
        input_content=input_content,
        response={"country": country},
    )

    return country


def lookup_maker_independence(
    session: Session, maker_name: str, country: str | None = None
) -> bool | None:
    """Ask Claude whether a watchmaker is independent or part of a conglomerate.

    Returns True (independent), False (conglomerate), or None (uncertain).

    The default `is_independent=True` on Maker creation means we only want
    to commit a False here when we're confident — falsely flagging a maker
    as conglomerate would silently remove them from rising_star/hidden_gem
    presets. The prompt nudges toward NONE-when-uncertain.

    Conversely, committing True when we're uncertain is less risky — the
    default is already True, so this only matters when the model is
    confident enough to assert it. We still cache True responses to avoid
    re-asking, but the practical effect on already-True-by-default rows
    is just to set the enrichment marker.

    Results are cached by (model, prompt_version, maker_name, country).
    """
    input_content = _normalize_for_hashing(
        {"maker": maker_name, "country": country or ""}
    )

    cached = get_cached(
        session=session,
        model=ENRICHMENT_MODEL,
        prompt_version=LOOKUP_INDEPENDENCE_PROMPT_VERSION,
        input_content=input_content,
    )
    if cached is not None:
        logger.debug("Independence lookup cache hit for %r", maker_name)
        val = cached.get("is_independent")
        # JSON cache may store None as null; pass through.
        return val

    logger.info("Looking up independence for %r via LLM", maker_name)

    country_hint = f" (based in {country})" if country else ""
    user_prompt = (
        f"Is the watchmaker or watch brand {maker_name!r}{country_hint} an "
        f"independent watchmaker, or is it owned by a larger conglomerate?\n\n"
        f"Known conglomerates include but are not limited to: Richemont "
        f"(IWC, Lange, Vacheron, JLC, Cartier, Panerai, Piaget, Roger Dubuis, "
        f"Baume & Mercier, Montblanc), LVMH (TAG Heuer, Hublot, Zenith, "
        f"Bulgari, Daniel Roth post-2023), Swatch Group (Omega, Longines, "
        f"Tissot, Hamilton, Rado, Breguet, Blancpain, Glashütte Original, "
        f"Jaquet Droz, Harry Winston), Kering (Girard-Perregaux, Ulysse "
        f"Nardin), Citizen Group (Citizen, Bulova, Frederique Constant, "
        f"Alpina), Seiko Holdings (Seiko, Grand Seiko, Credor).\n\n"
        f"Rules:\n"
        f"- Reply with exactly YES if the maker is independent (not owned "
        f"by any of the above or similar conglomerates).\n"
        f"- Reply with exactly NO if the maker is owned by a conglomerate.\n"
        f"- Reply with exactly NONE if you are not confident enough to "
        f"answer definitively. Prefer NONE over guessing.\n"
        f"- Reply with only YES, NO, or NONE — no other text.\n"
    )

    client = _get_client()
    message = client.messages.create(
        model=ENRICHMENT_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            raw_text += block.text

    cleaned = raw_text.strip().upper().rstrip(".,;)")
    is_independent: bool | None
    if cleaned == "YES":
        is_independent = True
    elif cleaned == "NO":
        is_independent = False
    elif cleaned == "NONE" or not cleaned:
        is_independent = None
    else:
        logger.info(
            "Independence lookup for %r returned unexpected %r — treating as NONE",
            maker_name, cleaned[:50],
        )
        is_independent = None

    set_cached(
        session=session,
        model=ENRICHMENT_MODEL,
        prompt_version=LOOKUP_INDEPENDENCE_PROMPT_VERSION,
        input_content=input_content,
        response={"is_independent": is_independent},
    )

    return is_independent
