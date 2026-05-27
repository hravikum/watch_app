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

import re
import unicodedata

from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from watch_app.storage.models import Maker


FUZZY_MATCH_THRESHOLD = 90  # 0-100; above this we consider it the same maker


# Same hyphen/whitespace collapse as extraction.entities._normalize.
# Keeps the two normalization functions in sync so an article-extracted
# candidate normalizes the same way as the seed entries it's matched
# against. Updated in 2W-hyphen-norm.
_HYPHEN_OR_WS = re.compile(r"[-\s]+")


def _normalize(name: str) -> str:
    """Case-fold, strip diacritics, collapse hyphens/whitespace.

    "Raúl Pagès" and "Raul Pages" should match.
    "Petermann-Bédat" and "Petermann Bédat" should match.
    """
    nfkd = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    folded = stripped.casefold()
    return _HYPHEN_OR_WS.sub(" ", folded).strip()


def resolve_or_create_maker(session: Session, candidate_name: str) -> Maker:
    """Find an existing Maker matching `candidate_name`, or create a new one.

    Matches against both canonical_name and aliases. Uses fuzzy matching so
    minor variants ("Simon Brette" vs "Simon Brette ") resolve together.
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
                    maker.aliases = [*(maker.aliases or []), candidate_name]
                    session.flush()
                return maker

    # Step 3: create a new maker.
    new_maker = Maker(canonical_name=candidate_name, aliases=[])
    session.add(new_maker)
    session.flush()  # populate id
    return new_maker
