"""Maker resolution: turn a candidate name string into a canonical Maker.

Strategy:
1. Exact match against Maker.canonical_name or any alias.
2. Fuzzy match (rapidfuzz) against the union of canonical names + aliases,
   accepting above a threshold.
3. If nothing matches, create a new Maker with the candidate as canonical_name.

This is deliberately simple for Phase 0. Phase 1 will add more sophisticated
rules (handling "Simon Brette" vs "Simon Brette Watchmaking", company suffixes,
unicode normalization for names like "Raúl Pagès" vs "Raul Pages", etc.).
"""
from __future__ import annotations

import logging
import re
import unicodedata

from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from watch_app.storage.models import BlockedMaker, Maker

logger = logging.getLogger(__name__)


FUZZY_MATCH_THRESHOLD = 90  # 0-100; above this we consider it the same maker


# Hyphen/whitespace collapse, shared in spirit with extraction.entities._normalize
# (which serves a different job — finding names in article text). This function
# is the resolution/dedup normalizer, so it also strips generic maker affixes.
_HYPHEN_OR_WS = re.compile(r"[-\s]+")
_PERIODS = re.compile(r"\.")

# Generic prefixes/suffixes that don't distinguish one maker from another.
# Stripped for MATCHING ONLY — a stored canonical_name is never rewritten.
# Deliberately tight: brand-defining tokens ("& Son", "& Söhne", "& Arpels",
# "& Friends") are NOT here. This pulls apart the false-positive clusters
# ("Masa & Co." vs "Jacob & Co.") and lands true variants ("Atelier Akrivia"
# ~ "Akrivia", "H. Moser & Cie." ~ "H. Moser") at the cheap exact-match step.
_LEADING_AFFIX = re.compile(r"^(?:atelier|manufacture|maison|montres|horlogerie)\s+")
_TRAILING_AFFIX = re.compile(
    r"\s+(?:&\s+)?(?:cie|co|sa|ag|gmbh|inc|ltd|llc|watches|watchmaking|watchcraft)$"
)


def _normalize(name: str) -> str:
    """Case-fold, strip diacritics and periods, collapse whitespace, then strip
    generic maker affixes. Matching-only — never mutates stored names.

    "Raúl Pagès" ~ "Raul Pages"; "F.P. Journe" ~ "FP Journe";
    "Atelier Akrivia" ~ "Akrivia"; "H. Moser & Cie." ~ "H. Moser".
    """
    nfkd = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    folded = stripped.casefold()
    no_periods = _PERIODS.sub("", folded)
    collapsed = _HYPHEN_OR_WS.sub(" ", no_periods).strip()

    out = _LEADING_AFFIX.sub("", collapsed)
    prev = ""
    while prev != out:  # repeat so trailing affix chains collapse fully
        prev = out
        out = _TRAILING_AFFIX.sub("", out).strip()
    return out or collapsed  # never reduce a name to the empty string


def _matches_blocklist(session: Session, normalized_candidate: str) -> str | None:
    """Return the blocked canonical name the candidate matches, or None.

    Applies the SAME semantics the resolver uses to match existing makers
    (normalized exact, then fuzzy >= FUZZY_MATCH_THRESHOLD across canonical
    names + aliases) — so anything that would have resolved into a deleted
    maker is blocked, including variant spellings. Only consulted at the
    create step; existing-maker matching is never gated.
    """
    blocked = session.query(BlockedMaker).all()
    if not blocked:
        return None

    lookup: dict[str, str] = {}
    for row in blocked:
        lookup[_normalize(row.canonical_name)] = row.canonical_name
        for alias in row.aliases or []:
            lookup[_normalize(alias)] = row.canonical_name

    if normalized_candidate in lookup:
        return lookup[normalized_candidate]

    best = process.extractOne(
        normalized_candidate, list(lookup.keys()), scorer=fuzz.WRatio
    )
    if best is not None:
        matched_key, score, _ = best
        if score >= FUZZY_MATCH_THRESHOLD:
            return lookup[matched_key]
    return None


def resolve_or_create_maker(session: Session, candidate_name: str) -> Maker | None:
    """Find an existing Maker matching `candidate_name`, or create a new one.

    Matches against both canonical_name and aliases. Uses fuzzy matching so
    minor variants ("Simon Brette" vs "Simon Brette ") resolve together.

    Returns None if the candidate matches the blocked_makers tombstone table
    (a hard-deleted maker) — the create step is gated so a deleted maker
    can't be resurrected by the next scan. See BlockedMaker.
    """
    candidate_name = candidate_name.strip()
    if not candidate_name:
        raise ValueError("candidate_name must be non-empty")

    normalized_candidate = _normalize(candidate_name)

    # Pull all makers into memory. Fine at this scale (hundreds to low thousands).
    # If this ever becomes a bottleneck, swap in a trigram index.
    all_makers = session.query(Maker).all()

    # Build a lookup: normalized_name -> Maker, across canonical + aliases.
    lookup: dict[str, Maker] = {}
    for maker in all_makers:
        lookup[_normalize(maker.canonical_name)] = maker
        for alias in maker.aliases or []:
            lookup[_normalize(alias)] = maker

    # Step 1: exact (normalized) match.
    if normalized_candidate in lookup:
        return lookup[normalized_candidate]

    # Step 2: fuzzy match.
    if lookup:
        best = process.extractOne(
            normalized_candidate,
            list(lookup.keys()),
            scorer=fuzz.WRatio,
        )
        if best is not None:
            matched_key, score, _ = best
            if score >= FUZZY_MATCH_THRESHOLD:
                maker = lookup[matched_key]
                # Remember the new spelling as an alias so next time is exact.
                if candidate_name not in (maker.aliases or []) and (
                    candidate_name != maker.canonical_name
                ):
                    logger.info(
                        "Fuzzy-matched %r to maker %r (score %.0f); recording as alias.",
                        candidate_name,
                        maker.canonical_name,
                        score,
                    )
                    maker.aliases = [*(maker.aliases or []), candidate_name]
                    session.flush()
                return maker

    # Step 3: create a new maker — unless the name is tombstoned.
    blocked_as = _matches_blocklist(session, normalized_candidate)
    if blocked_as is not None:
        logger.info(
            "Skipping creation of %r — matches blocked (deleted) maker %r.",
            candidate_name,
            blocked_as,
        )
        return None

    new_maker = Maker(canonical_name=candidate_name, aliases=[])
    session.add(new_maker)
    session.flush()  # populate id
    return new_maker
