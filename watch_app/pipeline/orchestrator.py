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
import re
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
from watch_app.storage.models import Mention, ScanRun, Source as SourceRow


logger = logging.getLogger(__name__)


def default_sources() -> list[Source]:
    """Return the list of sources to run by default.

    Current roster:
    - 5 RSS-based media sources (SJX, Monochrome, Fratello, Hodinkee, Quill & Pad)
    - 3 GPHG editions (2023, 2024, 2025) — award nominations
    - 3 Reddit subreddits (r/watchmaking, r/horology, r/Watches)
    - N YouTube channels (loaded from watch_app/data/youtube_channels.yaml)

    Tests and ad-hoc runs can import DummySource directly and pass it via the
    `sources=` argument of run_scan().
    """
    from watch_app.sources.awards.gphg import GPHGSource
    from watch_app.sources.media.fratello import FratelloSource
    from watch_app.sources.media.hodinkee import HodinkeeSource
    from watch_app.sources.media.monochrome import MonochromeSource
    from watch_app.sources.media.quill_and_pad import QuillAndPadSource
    from watch_app.sources.media.reddit import (
        RedditHorologySource,
        RedditWatchesSource,
        RedditWatchmakingSource,
    )
    from watch_app.sources.media.sjx import SJXSource
    from watch_app.sources.media.youtube import build_youtube_sources

    return [
        SJXSource(),
        MonochromeSource(),
        FratelloSource(),
        HodinkeeSource(),
        QuillAndPadSource(),
        GPHGSource(2025),
        GPHGSource(2024),
        GPHGSource(2023),
        RedditWatchmakingSource(),
        RedditHorologySource(),
        RedditWatchesSource(),
        *build_youtube_sources(),
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


def _extract_model_name_from_title(title: str | None) -> str | None:
    """GPHG mention titles look like:

        'GPHG 2025 nominee — Parmigiani Fleurier: Tonda PF Micro-Rotor Platinum'

    The model name is everything after the colon following the brand. If the
    title doesn't match this format we return None and the caller logs and
    skips.
    """
    if not title:
        return None
    if "—" not in title or ":" not in title:
        return None
    after_dash = title.split("—", 1)[1]
    if ":" not in after_dash:
        return None
    return after_dash.split(":", 1)[1].strip() or None


#: User-agent for GPHG detail fetches. Same browser-like UA we use for the
#: index page — GPHG returns a stripped React shell to default crawler UAs.
_GPHG_DETAIL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


def _fetch_gphg_models(session: Session) -> int:
    """Fetch detail pages for all GPHG-source mentions whose URLs don't yet
    have a WatchModel row.

    Returns the number of new WatchModel rows created. A returned 0 is the
    steady state after the first scan that runs this code — every existing
    GPHG mention will already have a model.

    Failures on individual URLs are logged and skipped; one bad detail page
    doesn't disrupt the rest. We deliberately don't refetch existing rows:
    GPHG detail pages don't change after the year ends, and refetching them
    nightly would be wasteful and risk triggering rate limits.
    """
    from watch_app.http import get as http_get
    from watch_app.sources.awards.gphg_detail import parse_detail_page
    from watch_app.storage.models import WatchModel

    awards_source_ids = [
        sid for (sid,) in session.query(SourceRow.id)
        .filter(SourceRow.category == "awards").all()
    ]
    if not awards_source_ids:
        return 0

    existing_urls = {
        url for (url,) in session.query(WatchModel.gphg_url).all()
    }

    # Distinct (url, title, maker_id, source_id) — multiple Mention rows for
    # the same nominee URL across different makers shouldn't yield duplicate
    # WatchModel rows, but we deduplicate just in case.
    pending_q = (
        session.query(Mention.url, Mention.title, Mention.maker_id, Mention.source_id)
        .filter(Mention.source_id.in_(awards_source_ids))
    )
    if existing_urls:
        pending_q = pending_q.filter(~Mention.url.in_(existing_urls))
    pending = pending_q.distinct().all()
    if not pending:
        return 0

    # Map source_id → year, parsed from the source name like 'GPHG 2025'.
    source_years: dict[int, int | None] = {}
    for sid in awards_source_ids:
        src = session.get(SourceRow, sid)
        year_match = (
            re.search(r"\b(20\d{2})\b", src.name) if src and src.name else None
        )
        source_years[sid] = int(year_match.group(1)) if year_match else None

    created = 0
    logger.info("Fetching detail pages for %d new GPHG models", len(pending))
    seen_in_this_run: set[str] = set()
    for url, title, maker_id, source_id in pending:
        if url in seen_in_this_run:
            continue
        seen_in_this_run.add(url)

        try:
            response = http_get(url, user_agent=_GPHG_DETAIL_USER_AGENT)
            if response.status_code != 200:
                logger.warning(
                    "GPHG detail %s: HTTP %s", url, response.status_code,
                )
                continue
            specs = parse_detail_page(response.text)
        except Exception:
            logger.exception("Failed to fetch GPHG detail: %s", url)
            continue

        name = _extract_model_name_from_title(title)
        if not name:
            logger.warning("Could not parse model name from title: %r", title)
            continue

        slug = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        year = source_years.get(source_id)
        if year is None:
            logger.warning("Skipping GPHG model — no year on source: %s", url)
            continue

        session.add(
            WatchModel(
                maker_id=maker_id,
                name=name[:300],
                slug=slug[:200],
                gphg_year=year,
                gphg_url=url,
                image_url=specs.get("image_url"),
                case_material=specs.get("case_material"),
                case_diameter_mm=specs.get("case_diameter_mm"),
                movement_caliber=specs.get("movement_caliber"),
                movement_type=specs.get("movement_type"),
                frequency_hz=specs.get("frequency_hz"),
                power_reserve_hours=specs.get("power_reserve_hours"),
                water_resistance_m=specs.get("water_resistance_m"),
                complications=specs.get("complications"),
                price_chf=specs.get("price_chf"),
                limited_edition_count=specs.get("limited_edition_count"),
                description=specs.get("description"),
            )
        )
        created += 1

    return created


def run_scan(
    sources: Iterable[Source] | None = None,
    use_llm: bool = False,
    normalize: bool = True,
    trigger: str = "manual",
) -> dict[str, int]:
    """Run a full scan: ingest all sources, then recompute scores.

    Args:
        sources: Sources to run. Defaults to default_sources().
        use_llm: Enable LLM-backed entity extraction.
        normalize: Whether to z-score normalize signals before composing scores.
        trigger: Who invoked this scan — "manual", "scheduled", or "test".
            Recorded in the ScanRun row.

    Returns: {"mentions_written": N, "makers_scored": M, "sources_run": S, ...,
             "scan_run_id": ID}.

    Every call creates exactly one ScanRun row, recording timing, status, and
    counts. If the scan fails partway, the row is marked status='failed' with
    the exception message, and the row survives in its own transaction (we
    don't rollback history on failure).
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

    # Step 1: Create the ScanRun row in its own transaction so it survives
    # any subsequent failure.
    with session_scope() as meta_session:
        run_row = ScanRun(trigger=trigger, status="running")
        meta_session.add(run_row)
        meta_session.flush()
        scan_run_id = run_row.id
    stats["scan_run_id"] = scan_run_id

    try:
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

            # Fetch GPHG model detail pages for any new mentions. Cached,
            # so amortizes to zero cost after the first run that includes
            # this code.
            stats["models_fetched"] = _fetch_gphg_models(session)

            # Score under every preset so the UI can flip between them without
            # re-running.
            for preset_name in PRESETS:
                created = rank_all_makers(session, preset_name, normalize=normalize)
                stats["makers_scored"] = len(created)  # same count per preset
    except Exception as e:
        # Record failure in its own transaction.
        with session_scope() as meta_session:
            failed = meta_session.get(ScanRun, scan_run_id)
            if failed is not None:
                failed.status = "failed"
                failed.finished_at = datetime.now(timezone.utc)
                failed.error_message = f"{type(e).__name__}: {e}"[:4000]
        raise

    # Mark completed with final counts in its own transaction.
    with session_scope() as meta_session:
        completed = meta_session.get(ScanRun, scan_run_id)
        if completed is not None:
            completed.status = "completed"
            completed.finished_at = datetime.now(timezone.utc)
            completed.sources_run = stats["sources_run"]
            completed.mentions_written = stats["mentions_written"]
            completed.makers_scored = stats["makers_scored"]

    return stats
