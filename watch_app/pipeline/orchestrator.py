"""Pipeline orchestrator — the main `scan` loop.

Flow:
    For each source:
        For each RawMention from that source:
            (optionally) run LLM extraction to fill candidate_names
            For each candidate_name:
                Resolve to a canonical Maker (create if new)
                Upsert a Mention row (dedup on (source, url))
    Once all mentions are ingested:
        Recompute signals + scores for every maker under every preset

Returns a small stats dict for the CLI/API to display.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from watch_app.extraction.entities import (
    extract_maker_names,
    invalidate_vocab_cache,
)
from watch_app.extraction.resolver import resolve_or_create_maker
from watch_app.extraction.seed import load_seed_makers
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.ranker import rank_all_makers
from watch_app.sources.base import RawMention, Source
from watch_app.storage.db import session_scope
from watch_app.storage.models import Mention, Source as SourceRow


logger = logging.getLogger(__name__)


def default_sources() -> list[Source]:
    """Return the list of sources to run by default.

    Phase 1C: the five RSS-based sources. Each is lightweight (one HTTP call)
    and runs in a few seconds. More sources (GPHG, Reddit) come in later chunks.

    Tests and ad-hoc runs can import DummySource directly and pass it via the
    `sources=` argument of run_scan().
    """
    from watch_app.sources.media.fratello import FratelloSource
    from watch_app.sources.media.hodinkee import HodinkeeSource
    from watch_app.sources.media.monochrome import MonochromeSource
    from watch_app.sources.media.quill_and_pad import QuillAndPadSource
    from watch_app.sources.media.sjx import SJXSource

    return [
        SJXSource(),
        MonochromeSource(),
        FratelloSource(),
        HodinkeeSource(),
        QuillAndPadSource(),
    ]


def _upsert_source_row(session: Session, source: Source) -> SourceRow:
    """Get or create the DB row for a Source. Updates metadata each run."""
    row = session.query(SourceRow).filter_by(name=source.name).one_or_none()
    if row is None:
        row = SourceRow(
            name=source.name,
            category=source.category,
            prestige_weight=source.prestige_weight,
        )
        session.add(row)
        session.flush()
    else:
        row.category = source.category
        row.prestige_weight = source.prestige_weight
    row.last_scraped_at = datetime.now(timezone.utc)
    return row


def _ingest_mention(
    session: Session,
    raw: RawMention,
    source_row: SourceRow,
    use_llm: bool,
) -> int:
    """Ingest one RawMention. Returns the number of Mention rows written."""
    # If the source didn't tell us which makers this is about, try to figure
    # it out. Priority: LLM (if enabled) → rule-based extractor (free).
    if not raw.candidate_names:
        if use_llm:
            # Local import so skipping --use-llm doesn't require the anthropic SDK path.
            from watch_app.extraction.entities_llm import populate_candidates_from_llm

            populate_candidates_from_llm(session, raw)

        if not raw.candidate_names:
            # Rule-based fallback using the known-makers vocabulary.
            title = raw.title or ""
            body = raw.raw_text or raw.excerpt or ""
            raw.candidate_names = extract_maker_names(session, title, body)

    if not raw.candidate_names:
        logger.debug("Skipping mention with no candidates: %s", raw.url)
        return 0

    written = 0
    for candidate in raw.candidate_names:
        maker = resolve_or_create_maker(session, candidate)

        # Dedup on (source, url): if we've already recorded this article for
        # this maker from this source, skip.
        existing = (
            session.query(Mention)
            .filter_by(source_id=source_row.id, url=raw.url, maker_id=maker.id)
            .one_or_none()
        )
        if existing is not None:
            continue

        session.add(
            Mention(
                maker_id=maker.id,
                source_id=source_row.id,
                url=raw.url,
                title=raw.title,
                excerpt=raw.excerpt,
                raw_text=raw.raw_text,
                published_at=raw.published_at,
            )
        )
        written += 1

    return written


def run_scan(
    sources: Iterable[Source] | None = None,
    use_llm: bool = False,
) -> dict[str, int]:
    """Run a full scan: ingest all sources, then recompute scores.

    Returns: {"mentions_written": N, "makers_scored": M, "sources_run": S}
    """
    if sources is None:
        sources = default_sources()

    stats = {
        "mentions_written": 0,
        "makers_scored": 0,
        "sources_run": 0,
        "seed_created": 0,
        "seed_updated": 0,
    }

    with session_scope() as session:
        # Load curated known-makers first, so the rule-based extractor has
        # its vocabulary populated from the very first source.
        seed_stats = load_seed_makers(session)
        stats["seed_created"] = seed_stats["created"]
        stats["seed_updated"] = seed_stats["updated"]
        session.flush()
        # Any change to the Makers table invalidates the extractor's cached
        # vocabulary. Drop it so _get_vocab rebuilds on the next call.
        invalidate_vocab_cache()

        for source in sources:
            logger.info("Fetching from source: %s", source.name)
            source_row = _upsert_source_row(session, source)

            try:
                raws = list(source.fetch())
            except Exception:
                logger.exception("Source %s failed; skipping", source.name)
                continue

            for raw in raws:
                stats["mentions_written"] += _ingest_mention(
                    session=session,
                    raw=raw,
                    source_row=source_row,
                    use_llm=use_llm,
                )

            stats["sources_run"] += 1

        # Score under every preset so the UI can flip between them without
        # re-running.
        for preset_name in PRESETS:
            created = rank_all_makers(session, preset_name)
            stats["makers_scored"] = len(created)  # same count per preset

    return stats
