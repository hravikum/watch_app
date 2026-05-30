"""Controlled vocabulary for maker taste / character dimensions (chunk 3A).

These power the "Profile" section on the maker detail page and, later, the
multi-dimension explorer (Layer 2). There are two kinds of dimension:

- Multi-tag (a maker can carry several): ``aesthetic_tags``,
  ``complication_focus``.
- Single-value (one token): ``maker_type``, ``price_band``, ``output_scale``,
  ``distribution``, ``movement_origin``.

The vocabulary is intentionally small. Adding a value here is cheap; renaming
or removing one after makers are tagged is not — so expand deliberately. See
the "Open design questions" note in NOTES.md for the reasoning.

``validate_taste_fields`` is the single source of truth for what's allowed.
The seed loader calls it per entry; ``tests/test_taste.py`` runs it across the
whole committed YAML so a typo fails the suite rather than silently
fragmenting the vocabulary.
"""
from __future__ import annotations


# Multi-tag dimensions: the YAML value is a list; every element must be in the
# dimension's allowed set.
MULTI_VALUE_FIELDS: tuple[str, ...] = ("aesthetic_tags", "complication_focus")

# Single-value dimensions: the YAML value is one string from the allowed set.
SINGLE_VALUE_FIELDS: tuple[str, ...] = (
    "maker_type",
    "price_band",
    "output_scale",
    "distribution",
    "movement_origin",
)

# Display order on the profile panel (aesthetic first, then identity, then the
# practical-fit fields, then movement).
TASTE_FIELDS: tuple[str, ...] = (
    "aesthetic_tags",
    "complication_focus",
    "maker_type",
    "price_band",
    "output_scale",
    "distribution",
    "movement_origin",
)


# Allowed values per dimension. Tokens are what's stored in the DB and YAML;
# DISPLAY_LABELS turns them into human-readable text for the UI.
VOCABULARY: dict[str, tuple[str, ...]] = {
    "aesthetic_tags": (
        "classical",
        "modern",
        "avant-garde",
        "vintage-inspired",
        "tool",
        "dressy",
    ),
    "complication_focus": (
        "time-only",
        "chronograph",
        "tourbillon",
        "calendar",
        "chiming",
        "astronomical",
    ),
    "maker_type": (
        "solo-watchmaker",
        "small-atelier",
        "heritage-relaunch",
        "microbrand",
        "established-indie",
    ),
    "price_band": ("entry", "mid", "high", "haute"),
    "output_scale": ("one-off", "sub-50", "50-500", "500-plus"),
    "distribution": ("direct", "dealer-only", "gallery", "allocation-waitlist"),
    "movement_origin": ("in-house", "modified-base", "stock-base"),
}


# Human-readable labels for tokens that aren't self-explanatory. Tokens not
# listed here fall back to a hyphen-stripped capitalization.
DISPLAY_LABELS: dict[str, str] = {
    "avant-garde": "Avant-garde",
    "vintage-inspired": "Vintage-inspired",
    "time-only": "Time-only",
    "solo-watchmaker": "Solo watchmaker",
    "small-atelier": "Small atelier",
    "heritage-relaunch": "Heritage relaunch",
    "established-indie": "Established indie",
    "entry": "Entry (<$2k)",
    "mid": "Mid ($2k\u201310k)",
    "high": "High ($10k\u201350k)",
    "haute": "Haute ($50k+)",
    "one-off": "One-off",
    "sub-50": "Sub-50 / yr",
    "50-500": "50\u2013500 / yr",
    "500-plus": "500+ / yr",
    "dealer-only": "Dealer-only",
    "allocation-waitlist": "Allocation / waitlist",
    "in-house": "In-house",
    "modified-base": "Modified base",
    "stock-base": "Stock base",
}

# Section label for each dimension on the profile panel.
FIELD_LABELS: dict[str, str] = {
    "aesthetic_tags": "Aesthetic",
    "complication_focus": "Known for",
    "maker_type": "Type",
    "price_band": "Price band",
    "output_scale": "Output",
    "distribution": "Access",
    "movement_origin": "Movement",
}


def display_label(token: str) -> str:
    """Return the human-readable label for a vocabulary token."""
    return DISPLAY_LABELS.get(token, token.replace("-", " ").capitalize())


def sanitize_taste(raw: dict) -> dict:
    """Coerce a raw (possibly LLM-produced) taste dict to in-vocabulary values.

    For each taste field, keep only values present in the controlled
    vocabulary and drop the rest. Multi-value fields become a de-duplicated
    filtered list (omitted entirely if nothing valid survives); single-value
    fields are kept only if valid. Non-taste keys are ignored. The returned
    dict contains only fields with a non-empty valid value and is guaranteed
    to pass ``validate_taste_fields``.
    """
    clean: dict = {}

    for field in MULTI_VALUE_FIELDS:
        value = raw.get(field)
        if not isinstance(value, list):
            continue
        allowed = VOCABULARY[field]
        kept: list[str] = []
        for v in value:
            if v in allowed and v not in kept:
                kept.append(v)
        if kept:
            clean[field] = kept

    for field in SINGLE_VALUE_FIELDS:
        value = raw.get(field)
        if isinstance(value, str) and value in VOCABULARY[field]:
            clean[field] = value

    return clean


def validate_taste_fields(entry: dict) -> None:
    """Raise ``ValueError`` if any taste field in ``entry`` is out of vocabulary.

    Fields that are absent or ``None`` are ignored — taste data is optional.
    Multi-value fields must be lists; single-value fields must be strings.
    Every problem is collected into one message so a curator can fix them all
    at once rather than one failed load at a time.
    """
    problems: list[str] = []

    for field in MULTI_VALUE_FIELDS:
        value = entry.get(field)
        if value is None:
            continue
        if not isinstance(value, list):
            problems.append(f"{field} must be a list, got {type(value).__name__}")
            continue
        allowed = VOCABULARY[field]
        for v in value:
            if v not in allowed:
                problems.append(f"{field}: unknown value {v!r}")

    for field in SINGLE_VALUE_FIELDS:
        value = entry.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            problems.append(f"{field} must be a string, got {type(value).__name__}")
            continue
        if value not in VOCABULARY[field]:
            problems.append(f"{field}: unknown value {value!r}")

    if problems:
        name = entry.get("canonical_name", "<unknown>")
        raise ValueError(
            f"Invalid taste fields for {name!r}: " + "; ".join(problems)
        )
