"""Rule-based entity extractor.

Given article text, finds mentions of known makers from the seed list.

Strategy:
1. Build a combined vocabulary of all canonical names + aliases from the
   DB (on-demand, cached per process).
2. For each term, search for whole-word matches in the title and body.
   "Whole word" means not embedded in a larger word — "MB" shouldn't
   match inside "MBA".
3. Normalize for diacritics so "Raúl Pagès" matches "Raul Pages".
4. Return the canonical names of all matched makers, deduplicated.

Limitations — acceptable for Phase 1:
- Won't discover makers who aren't in the seed list. That's fine: the
  LLM path (--use-llm) handles discovery; this path handles confirmation.
- Case-insensitive, so "Nomos" matches "nomos" in forum posts.
- Multi-word names like "Petermann Bédat" are matched as phrases, not
  as individual tokens.

Performance: matching 100 terms against a typical article (~2000 chars)
takes well under 1ms. We don't need Aho-Corasick or similar yet.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from threading import Lock

from sqlalchemy.orm import Session

from watch_app.storage.models import Maker


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VocabEntry:
    """A single searchable term → canonical maker name mapping."""

    term_normalized: str
    canonical_name: str
    pattern: re.Pattern[str]


# --- Vocabulary cache ---
# Rebuilt whenever the DB changes are "material" enough to matter. In Phase 1
# we just rebuild on every scan; the cost is trivial.

_vocab_lock = Lock()
_vocab_cache: list[VocabEntry] | None = None


def _normalize(text: str) -> str:
    """Lowercase + strip combining diacritics."""
    nfkd = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return stripped.casefold()


def _build_pattern(term: str) -> re.Pattern[str]:
    """Whole-word match for a normalized term.

    We use custom boundaries rather than \\b because \\b doesn't play
    well with some punctuation and apostrophes in watch names.

    The boundary: either string start/end, or a non-alphanumeric character.
    This ensures "MB" doesn't match inside "MBA" but does match in "MB&F".
    """
    escaped = re.escape(term)
    # Boundaries: must not be preceded/followed by another word character.
    return re.compile(
        rf"(?:(?<=^)|(?<=[^A-Za-z0-9])){escaped}(?=[^A-Za-z0-9]|$)",
        re.IGNORECASE,
    )


def _build_vocab(session: Session) -> list[VocabEntry]:
    """Build the full vocabulary from the Makers table."""
    entries: list[VocabEntry] = []
    makers = session.query(Maker).all()
    for maker in makers:
        terms = [maker.canonical_name] + list(maker.aliases or [])
        for term in terms:
            if not term or not term.strip():
                continue
            normalized = _normalize(term)
            entries.append(
                VocabEntry(
                    term_normalized=normalized,
                    canonical_name=maker.canonical_name,
                    pattern=_build_pattern(normalized),
                )
            )
    # Sort by length descending so longer names get first shot at matching.
    # "Petermann Bédat" should match before "Bédat" alone — important when
    # a maker's surname appears in another maker's alias.
    entries.sort(key=lambda e: len(e.term_normalized), reverse=True)
    logger.debug("Built extractor vocabulary with %d entries", len(entries))
    return entries


def invalidate_vocab_cache() -> None:
    """Drop the cached vocabulary. Called after the seed loader runs."""
    global _vocab_cache
    with _vocab_lock:
        _vocab_cache = None


def _get_vocab(session: Session) -> list[VocabEntry]:
    global _vocab_cache
    with _vocab_lock:
        if _vocab_cache is None:
            _vocab_cache = _build_vocab(session)
        return _vocab_cache


def extract_maker_names(session: Session, title: str, text: str) -> list[str]:
    """Return canonical names of makers mentioned in title + text.

    Deduplicated. Order: by first occurrence position in the combined text.
    """
    vocab = _get_vocab(session)
    if not vocab:
        return []

    combined = _normalize(f"{title or ''}\n{text or ''}")
    if not combined.strip():
        return []

    found: dict[str, int] = {}  # canonical_name -> earliest position
    # To avoid a long maker's name being "eaten" by a shorter alias later in
    # the vocab, we remove matched regions from the search space as we go.
    # Use a list of (start, end) spans to mark consumed regions.
    consumed: list[tuple[int, int]] = []

    def _overlaps_consumed(start: int, end: int) -> bool:
        for cs, ce in consumed:
            if start < ce and end > cs:
                return True
        return False

    for entry in vocab:  # longest-first
        for match in entry.pattern.finditer(combined):
            start, end = match.span()
            if _overlaps_consumed(start, end):
                continue
            consumed.append((start, end))
            if entry.canonical_name not in found or start < found[entry.canonical_name]:
                found[entry.canonical_name] = start

    return [name for name, _ in sorted(found.items(), key=lambda kv: kv[1])]
