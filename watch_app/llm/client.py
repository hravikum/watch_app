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
from urllib.parse import urlparse

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


# v2 (3G): the prompt now accepts an optional `context` block of recent press
# mentions, which sharply reduces abstention on niche makers the model doesn't
# know by name alone. Bumped from v1 so context-aware lookups don't collide
# with the old bare-name cache entries.
LOOKUP_TASTE_PROMPT_VERSION = "v2-2026-05-30"


def lookup_maker_taste(
    session: Session,
    maker_name: str,
    country: str | None = None,
    context: str | None = None,
) -> dict:
    """Ask Claude to draft taste / character tags for a watchmaker.

    Returns a dict with up to seven keys (the taste dimensions). Values are
    vocabulary tokens (or lists of tokens), or null where the model isn't
    confident. The caller MUST sanitize the result against the controlled
    vocabulary (``taste.sanitize_taste``) — this function does not guarantee
    in-vocabulary output, since the model may invent tokens.

    ``context`` is an optional block of recent press mentions (titles +
    excerpts) for the maker. For niche makers the model doesn't recognize by
    name, this is the difference between a confident tag and a blank
    abstention — see the Barrelhand case in chunk 3G.

    Results are cached by (model, prompt_version, maker_name, country,
    context). The prompt allows null per field and biases toward abstention on
    output scale / distribution / movement origin, which are hard to know for
    niche makers — a gap is preferable to a wrong tag.
    """
    from watch_app import taste

    input_content = _normalize_for_hashing(
        {"maker": maker_name, "country": country or "", "context": context or ""}
    )

    cached = get_cached(
        session=session,
        model=ENRICHMENT_MODEL,
        prompt_version=LOOKUP_TASTE_PROMPT_VERSION,
        input_content=input_content,
    )
    if cached is not None:
        logger.debug("Taste lookup cache hit for %r", maker_name)
        return cached

    logger.info("Looking up taste for %r via LLM", maker_name)

    def opts(field: str) -> str:
        return ", ".join(taste.VOCABULARY[field])

    country_hint = f" (based in {country})" if country else ""
    context_block = ""
    if context:
        context_block = (
            f"\nRecent press mentions of this maker (use them to inform your "
            f"tags — they often reveal style, complications, scale and origin):\n"
            f"{context}\n"
        )
    user_prompt = (
        f"You are tagging the independent watchmaker or brand {maker_name!r}"
        f"{country_hint} with character tags for a watch-discovery tool.\n"
        f"{context_block}\n"
        f"Return a JSON object with exactly these keys. Use ONLY the listed "
        f"values. Use null (or [] for the list fields) for anything you are "
        f"not confident about — a missing tag is far better than a wrong one.\n\n"
        f"- aesthetic_tags (list, 1-3 of): {opts('aesthetic_tags')}\n"
        f"- complication_focus (list, 1-3 of) — what they're known for: "
        f"{opts('complication_focus')}\n"
        f"- maker_type (one of): {opts('maker_type')}\n"
        f"- price_band (one of): {opts('price_band')} "
        f"(entry <$2k, mid $2k-10k, high $10k-50k, haute $50k+)\n"
        f"- output_scale (one of) — watches per year: {opts('output_scale')}\n"
        f"- distribution (one of): {opts('distribution')}\n"
        f"- movement_origin (one of): {opts('movement_origin')} "
        f"(in-house = own movements, modified-base = reworked stock ebauche, "
        f"stock-base = unmodified stock movement)\n\n"
        f"Be especially willing to use null for output_scale, distribution, "
        f"and movement_origin unless you specifically know them for this maker.\n"
        f"Reply with only the JSON object, no other text.\n"
    )

    client = _get_client()
    message = client.messages.create(
        model=ENRICHMENT_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            raw_text += block.text

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            parsed = {}
    except json.JSONDecodeError:
        logger.warning(
            "Taste lookup for %r returned non-JSON: %r", maker_name, raw_text[:200]
        )
        parsed = {}

    set_cached(
        session=session,
        model=ENRICHMENT_MODEL,
        prompt_version=LOOKUP_TASTE_PROMPT_VERSION,
        input_content=input_content,
        response=parsed,
    )

    return parsed


# --- Web-researched audit (chunk 4O) ---------------------------------------
#
# One call per maker with the server-side web search tool, so the model looks
# the brand up instead of answering from memory — the search a human otherwise
# ran for every /review card. Uses the basic tool version (web_search_20250305):
# the newer versions run search through code execution, whose response blocks
# the pinned SDK (0.39.0) may not parse. The SDK passes the tool definition
# through as plain JSON and constructs unknown response blocks leniently (their
# type, content and citations survive as attributes / dicts), which is all this
# needs — so no SDK bump.

#: Model for the audit. Newer than ENRICHMENT_MODEL, supports web search, and
#: its tokens cost a tenth of Haiku 4.5's — the per-search fee dominates the
#: cost either way. The other lookups stay on ENRICHMENT_MODEL (their cache is
#: keyed by model, so switching them would re-bill every cached answer).
AUDIT_MODEL = "claude-haiku-5-5"
AUDIT_PROMPT_VERSION = "v5-2026-11-26"
#: Cap on searches per maker — each one costs WEB_SEARCH_PRICE on top of tokens.
AUDIT_MAX_SEARCHES = 3
#: Room for any prose the model writes before it calls the answer tool. The
#: first live run (v1, 1,500 tokens, answer as a JSON object at the end of the
#: reply) never reached a parseable answer; output tokens are cheap here.
AUDIT_MAX_TOKENS = 4096
#: The client tool the model answers through (see _audit_tool).
AUDIT_TOOL_NAME = "record_audit"
AUDIT_VERDICTS: tuple[str, ...] = ("yes", "no", "unsure")

#: Prices for the CLI's cost line only (USD, checked 2026-11-26 against
#: https://platform.claude.com/docs/en/about-claude/pricing). An estimate — the
#: billing console is the source of truth.
AUDIT_PRICE_PER_MTOK_IN = 0.10
AUDIT_PRICE_PER_MTOK_OUT = 0.50
WEB_SEARCH_PRICE = 0.01

#: The drafted fields an audit returns, in card order.
AUDIT_FIELD_NAMES: tuple[str, ...] = (
    "country",
    "founded_year",
    "website",
    "is_independent",
    "aesthetic_tags",
    "complication_focus",
    "maker_type",
    "price_band",
    "output_scale",
    "distribution",
    "movement_origin",
)


def audit_cost(usage: dict) -> float:
    """Estimated USD for one audit call's usage (tokens + searches)."""
    return (
        usage.get("input_tokens", 0) * AUDIT_PRICE_PER_MTOK_IN / 1_000_000
        + usage.get("output_tokens", 0) * AUDIT_PRICE_PER_MTOK_OUT / 1_000_000
        + usage.get("web_search_requests", 0) * WEB_SEARCH_PRICE
    )


def _field(obj, name: str):
    """Read `name` from an SDK object or a plain dict — the pinned SDK hands
    back the nested content of block types it doesn't know as dicts."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _last_json_object(text: str) -> dict | None:
    """The last top-level JSON object in `text` — the fallback for a reply
    that writes its answer out instead of calling the answer tool. The model
    may narrate before answering, and citations can split the answer across
    several text blocks (joined before this is called)."""
    decoder = json.JSONDecoder()
    found: dict | None = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        i = text.find("{", end)
    return found


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/")


def _site(url: str) -> str:
    """The site a URL belongs to, so two pages from one site count once."""
    return urlparse(url).netloc.lower().removeprefix("www.")


#: Text a model may write where it means "no value". The answer tool types
#: brand_name as a string, and the v2 prompt said "otherwise null", so the
#: second live run got the word "null" back as a brand name.
_NO_VALUE_TEXT = frozenset({"", "null", "none", "n/a", "unknown"})


def _without_blanks(answer: dict) -> dict:
    """The answer minus values that say nothing: None, empty text and stand-ins
    like "null" — at the top level and inside lists, dropping a list left
    empty. Nothing in the taste vocabulary uses these words."""

    def blank(value) -> bool:
        return value is None or (
            isinstance(value, str) and value.strip().lower() in _NO_VALUE_TEXT
        )

    clean: dict = {}
    for key, value in answer.items():
        if isinstance(value, list):
            value = [item for item in value if not blank(item)]
            if not value:
                continue
        elif blank(value):
            continue
        clean[key] = value
    return clean


def _audit_tool() -> dict:
    """The client tool the model answers through, next to web search in the
    same request. Its input comes back as structured data, so the answer can't
    get lost at the end of a long cited reply the way a trailing JSON object
    did on the first live run. (The API's JSON-output mode isn't an option:
    it refuses citations, and web search always has them on.) The schema gives
    types only: the prompt explains each field and its vocabulary, and
    enrich.py checks every value, so an out-of-vocabulary pick still surfaces
    as a vocabulary gap instead of being forced onto the nearest option."""
    from watch_app import taste

    text = {"type": "string"}
    text_list = {"type": "array", "items": {"type": "string"}}
    flag = {"type": "boolean"}
    properties = {
        "verdict": {"type": "string", "enum": list(AUDIT_VERDICTS)},
        "reason": text,
        "brand_name": text,
        "pre_launch": flag,
        "country": text,
        "founded_year": {"type": "integer"},
        "website": text,
        "is_independent": flag,
        **{
            f: text_list if f in taste.MULTI_VALUE_FIELDS else text
            for f in taste.TASTE_FIELDS
        },
        "sources": text_list,
    }
    return {
        "name": AUDIT_TOOL_NAME,
        "description": (
            "Record your verdict on the name and the card fields you could "
            "confirm. Call it once, after your searches."
        ),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": ["verdict", "reason"],
        },
    }


def _audit_prompt(maker_name: str, country: str | None, context: str | None) -> str:
    from watch_app import taste

    def opts(field: str) -> str:
        return ", ".join(taste.VOCABULARY[field])

    country_hint = f" (thought to be based in {country})" if country else ""
    context_block = ""
    if context:
        context_block = (
            "\n\nPress mentions that led the tool to this name (they may be "
            "about something else entirely):\n" + context + "\n\n"
            "Judge the name as these mentions use it. If they use it for "
            "something other than a maker — a watchmaking term or phrase "
            "(\"Split\" from \"split-seconds\"), or another company's watch — the "
            "verdict is \"no\" even if some brand of that name exists; say so in "
            "the reason. Mentions of a maker's own watches and models count as "
            "the maker."
        )
    countries = ", ".join(sorted(KNOWN_WATCHMAKING_COUNTRIES))
    return (
        f"You are checking {maker_name!r}{country_hint} for a tool that discovers "
        f"independent wristwatch makers. A scan found the name in the press, so "
        f"it may not be a brand at all. Use web search to find out what it is "
        f"before answering.{context_block}\n\n"
        f"Verdict:\n"
        f"- \"yes\": a real, active maker of wristwatches under its own name. "
        f"Group-owned brands count (ownership is a separate field). A brand that "
        f"has announced or presented watches but isn't shipping yet counts — set "
        f"pre_launch to true. A person's name counts only when it is itself the "
        f"brand's name.\n"
        f"- \"no\": anything else — a phrase, model name or partial name grabbed "
        f"by mistake; a person (founder, owner, watchmaker) whose brand goes by "
        f"another name; a customizer that reworks other brands' watches; a "
        f"retailer or dealer; a movement or parts supplier; a maker of only "
        f"clocks, pocket watches or kits; a quartz fashion brand; a house that "
        f"is defunct and only appears in auction or vintage coverage.\n"
        f"- \"unsure\": the search couldn't settle it.\n\n"
        f"When you've finished searching, answer by calling the "
        f"{AUDIT_TOOL_NAME} tool once, on its own — don't write your answer out "
        f"as text. Leave out any field you couldn't confirm — a missing value "
        f"is far better than a wrong one. For a \"no\", leave out everything "
        f"from country down. The fields:\n"
        f"- verdict: \"yes\", \"no\" or \"unsure\"\n"
        f"- reason: one short sentence explaining the verdict\n"
        f"- brand_name: only when the name belongs to a brand that goes by a "
        f"different name — its founder or owner, a short form of its name, one "
        f"of its own models — that brand's name: the name will count as that "
        f"brand from now on. Leave it out for a general term or phrase, or a "
        f"name the brand doesn't own (say, a designer another company's watch "
        f"pays homage to), even when these mentions are about one brand's "
        f"watch\n"
        f"- pre_launch: true if announced but not shipping yet, false if "
        f"shipping\n"
        f"- sources: for every verdict, a \"no\" included — the URLs, from your "
        f"search results, of the two or three pages that best support it, "
        f"each from a different site\n"
        f"- country: one of: {countries}\n"
        f"- founded_year: the year the brand was founded, as a number\n"
        f"- website: the brand's own official website (not a retailer, "
        f"marketplace, social media or press page)\n"
        f"- is_independent: true if independent, false if owned by a larger "
        f"group\n"
        f"- aesthetic_tags (list, 1-3 of): {opts('aesthetic_tags')}\n"
        f"- complication_focus (list, 1-3 of) — what they're known for: "
        f"{opts('complication_focus')}\n"
        f"- maker_type (one of): {opts('maker_type')}\n"
        f"- price_band (one of): {opts('price_band')} (entry <$2k, mid $2k-10k, "
        f"high $10k-50k, haute $50k+)\n"
        f"- output_scale (one of) — watches per year: {opts('output_scale')}\n"
        f"- distribution (one of): {opts('distribution')}\n"
        f"- movement_origin (one of): {opts('movement_origin')} (in-house = "
        f"own movements, modified-base = reworked stock ebauche, stock-base = "
        f"unmodified stock movement)\n"
    )


def lookup_maker_audit(
    session: Session,
    maker_name: str,
    country: str | None = None,
    context: str | None = None,
) -> dict | None:
    """Research one maker on the web and draft its review card (chunk 4O).

    One Messages call with two tools: web search (at most AUDIT_MAX_SEARCHES
    searches) and AUDIT_TOOL_NAME, which the model calls with its answer. A
    reply that writes the answer out as a JSON object instead is still read.
    Returns a dict with:

    - verdict / reason / brand_name / pre_launch — as the model gave them
    - fields — the drafted values that came back non-null (AUDIT_FIELD_NAMES),
      UNVERIFIED: the caller must check and sanitize them (enrich.py does)
    - sources — [{url, title}], at most three and one per site, limited to URLs
      that appeared in this response's search results so a source can't be
      invented: the URLs the model listed first, topped up with the ones its
      answer cited
    - usage — input/output tokens and web_search_requests, for the cost line
    - cached — True when served from the cache (free)

    Returns None, and caches nothing, when the call produced no usable answer —
    stopped early (pause_turn; or max_tokens, since an answer cut off part-way
    can't be trusted), no answer at all, or no valid verdict — so a re-run
    tries again. The warning it logs says which, with the end of the reply.
    Otherwise cached by (model, prompt_version, name, country, context) like the
    other lookups; the caller commits, so a long run keeps what it has paid for.
    """
    input_content = _normalize_for_hashing(
        {"maker": maker_name, "country": country or "", "context": context or ""}
    )
    cached = get_cached(
        session=session,
        model=AUDIT_MODEL,
        prompt_version=AUDIT_PROMPT_VERSION,
        input_content=input_content,
    )
    if cached is not None:
        logger.debug("Audit cache hit for %r", maker_name)
        return {**cached, "cached": True}

    logger.info("Researching %r via LLM + web search", maker_name)
    client = _get_client()
    message = client.messages.create(
        model=AUDIT_MODEL,
        max_tokens=AUDIT_MAX_TOKENS,
        messages=[
            {"role": "user", "content": _audit_prompt(maker_name, country, context)}
        ],
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": AUDIT_MAX_SEARCHES,
            },
            _audit_tool(),
        ],
    )

    if message.stop_reason in ("pause_turn", "max_tokens"):
        logger.warning(
            "Audit of %r stopped early (%s) — no result; it will be retried",
            maker_name,
            message.stop_reason,
        )
        return None

    text = ""
    answer: dict | None = None
    cited: list[str] = []
    result_urls: dict[str, str] = {}    # normalized url -> url as returned
    result_titles: dict[str, str] = {}  # normalized url -> page title
    for block in message.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text += block.text
            for citation in getattr(block, "citations", None) or []:
                url = _field(citation, "url")
                if url:
                    cited.append(url)
        elif btype == "tool_use" and getattr(block, "name", None) == AUDIT_TOOL_NAME:
            if isinstance(getattr(block, "input", None), dict):
                answer = block.input
        elif btype == "web_search_tool_result":
            content = getattr(block, "content", None)
            if isinstance(content, list):  # an error result is a single dict
                for item in content:
                    url = _field(item, "url")
                    if url:
                        result_urls.setdefault(_norm_url(url), url)
                        result_titles.setdefault(_norm_url(url), _field(item, "title") or "")

    server_use = _field(message.usage, "server_tool_use") or {}
    searches = _field(server_use, "web_search_requests") or 0
    parsed = answer if answer is not None else _last_json_object(text)
    if parsed is not None:
        parsed = _without_blanks(parsed)
    if parsed is None or parsed.get("verdict") not in AUDIT_VERDICTS:
        logger.warning(
            "Audit of %r gave no usable answer (stop_reason=%s, %s search(es), %s); "
            "the reply ended: %r",
            maker_name,
            message.stop_reason,
            searches,
            "no answer" if parsed is None else f"verdict {parsed.get('verdict')!r}",
            text[-300:],
        )
        return None

    listed = parsed.get("sources")
    sources: list[dict] = []
    for url in (listed if isinstance(listed, list) else []) + cited:
        if not isinstance(url, str):
            continue
        key = _norm_url(url)
        if key in result_urls and all(
            _site(s["url"]) != _site(result_urls[key]) for s in sources
        ):
            sources.append({"url": result_urls[key], "title": result_titles.get(key, "")})
        if len(sources) == 3:
            break

    result = {
        "verdict": parsed.get("verdict"),
        "reason": parsed.get("reason"),
        "brand_name": parsed.get("brand_name"),
        "pre_launch": parsed.get("pre_launch"),
        "fields": {
            f: parsed[f] for f in AUDIT_FIELD_NAMES if parsed.get(f) is not None
        },
        "sources": sources,
        "usage": {
            "input_tokens": _field(message.usage, "input_tokens") or 0,
            "output_tokens": _field(message.usage, "output_tokens") or 0,
            "web_search_requests": searches,
        },
    }
    set_cached(
        session=session,
        model=AUDIT_MODEL,
        prompt_version=AUDIT_PROMPT_VERSION,
        input_content=input_content,
        response=result,
    )
    return {**result, "cached": False}
