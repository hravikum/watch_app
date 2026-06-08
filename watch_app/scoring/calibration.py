"""Source-quality calibration — a diagnostic for prestige-weight tuning.

Read-only analysis that turns accumulated mention data into a per-source
signal-quality readout, so the Phase-1 prestige-weight guesses (SJX 1.0,
Monochrome 0.9, Hodinkee 0.7, the YouTube tiers 0.3-0.9) can be checked
against what each source actually contributes.

Diagnostic ONLY. This module changes nothing and recommends nothing
automatically — it surfaces the evidence so that a weight adjustment becomes a
separate, deliberate decision (data before fix). It deliberately does not write
weights, suggest weights, or touch scoring.

Metrics per source, over the chosen window (default all-time), counting only
non-suppressed makers:

  - total_mentions  : raw volume the source produced.
  - distinct_makers : breadth (distinct makers touched).
  - first_surfaced  : makers this source covered before any other EDITORIAL /
                      COMMUNITY source did — i.e. the earliest non-awards source
                      to mention each maker, by `discovered_at`. This counts
                      both seed and discovered makers: a source being first to
                      cover even a curated maker over the scan window is a real
                      "ahead of the pack" signal (3Z corrected an earlier
                      version that wrongly excluded seed makers and zeroed the
                      column on seed-dominated data). High volume with few
                      firsts is mostly re-coverage.

                      AWARDS sources (GPHG editions) are excluded from this
                      race and always read 0 here. The editions are bulk-
                      ingested historically, so by timing artifact they would
                      "win" first for nearly every maker they nominated —
                      meaningless for calibrating editorial prestige, and an
                      awards source doesn't do discovery in the first place. A
                      maker mentioned only by awards sources is credited to no
                      one (correctly: no editorial source surfaced it).
  - first_ratio     : first_surfaced / total_mentions — discovery yield per
                      mention. The denominator is the source's whole volume on
                      purpose: prestige_weight is a per-mention multiplier, so
                      "makers surfaced first per mention" is the quantity it
                      should track. A high-volume source with a low ratio is
                      mostly echo. Always 0 for awards sources.
  - prestige_weight : the current per-source multiplier, shown beside the
                      evidence so over- and under-weighting is visible at a
                      glance.

Caveats (the CLI repeats these for the reader):
  - Suppressed makers are excluded from every count — their mentions are noise
    we've already decided to hide, so they shouldn't count toward a source's
    measured contribution.
  - Sources added mid-history (YouTube 2N, Reddit 2R) had a shorter window in
    which to be "first"; over a fully-active window that's minor.
  - When a window is given, first_surfaced means "earliest mention WITHIN the
    window", not the maker's true all-time first source. With no window
    (the default) it is the true all-time first source.

The first-surfaced attribution mirrors build_source_contributions in
methodology.py: per maker, the (non-awards) source with the earliest mention by
`discovered_at`; ties broken by MIN(source_id) so the total first_surfaced
across sources equals the count of non-suppressed makers that have at least one
non-awards mention.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from watch_app.storage.models import Maker, Mention, Source as SourceRow


@dataclass
class SourceQuality:
    """One row in the source-quality readout."""

    source_id: int
    source_name: str
    category: str
    prestige_weight: float
    total_mentions: int
    distinct_makers: int
    first_surfaced: int  # discovered (non-seed) makers surfaced first by this source
    first_ratio: float  # first_surfaced / total_mentions; 0.0 when no mentions


def compute_source_quality(
    session: Session, window_days: int | None = None
) -> list[SourceQuality]:
    """Compute the per-source signal-quality readout.

    Returns one row per source (including zero-mention sources, so a silent
    configured source is visible), ordered by total_mentions descending with
    source name as a stable tie-break.

    When `window_days` is given, every count is restricted to mentions whose
    `discovered_at` is within the last N days (and first_surfaced becomes
    first-within-window). Default (None) is all-time.

    Suppressed makers are excluded from all counts. first_surfaced credits the
    earliest non-awards source to mention each maker (seed or discovered);
    awards sources are excluded from that race and always read 0.
    """
    cutoff: datetime | None = None
    if window_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # Awards sources (GPHG editions) are bulk-ingested historically, so by
    # timing artifact they'd "win" first for nearly every maker they nominated.
    # Exclude them from the first-surfaced race entirely.
    awards_source_ids = [
        sid
        for (sid,) in session.query(SourceRow.id).filter(
            SourceRow.category == "awards"
        )
    ]

    def _maker_filters() -> list:
        conds = [Maker.is_suppressed.is_(False)]
        if cutoff is not None:
            conds.append(Mention.discovered_at >= cutoff)
        return conds

    # Total mentions per source, over non-suppressed makers within the window.
    totals = dict(
        session.query(Mention.source_id, func.count(Mention.id))
        .join(Maker, Maker.id == Mention.maker_id)
        .filter(*_maker_filters())
        .group_by(Mention.source_id)
        .all()
    )

    # Distinct makers per source.
    distincts = dict(
        session.query(
            Mention.source_id, func.count(func.distinct(Mention.maker_id))
        )
        .join(Maker, Maker.id == Mention.maker_id)
        .filter(*_maker_filters())
        .group_by(Mention.source_id)
        .all()
    )

    # First-surfaced per source, over non-suppressed makers and NON-awards
    # mentions. Both subqueries share the same filter set so attribution stays
    # inside the editorial/community universe — otherwise an awards mention at
    # the same earliest timestamp could win the MIN(source_id) tie-break.
    def _first_filters() -> list:
        conds = [Maker.is_suppressed.is_(False)]
        if awards_source_ids:
            conds.append(Mention.source_id.notin_(awards_source_ids))
        if cutoff is not None:
            conds.append(Mention.discovered_at >= cutoff)
        return conds

    earliest_per_maker = (
        session.query(
            Mention.maker_id,
            func.min(Mention.discovered_at).label("first_at"),
        )
        .join(Maker, Maker.id == Mention.maker_id)
        .filter(*_first_filters())
        .group_by(Mention.maker_id)
        .subquery()
    )
    # Tie-break: lowest source_id among (non-awards) mentions at that earliest
    # timestamp, so each maker is attributed to exactly one source.
    surfacing_source = (
        session.query(
            Mention.maker_id,
            func.min(Mention.source_id).label("source_id"),
        )
        .join(Maker, Maker.id == Mention.maker_id)
        .join(
            earliest_per_maker,
            (Mention.maker_id == earliest_per_maker.c.maker_id)
            & (Mention.discovered_at == earliest_per_maker.c.first_at),
        )
        .filter(*_first_filters())
        .group_by(Mention.maker_id)
        .subquery()
    )
    first_by_source = dict(
        session.query(
            surfacing_source.c.source_id,
            func.count(surfacing_source.c.maker_id),
        )
        .group_by(surfacing_source.c.source_id)
        .all()
    )

    rows: list[SourceQuality] = []
    for s in session.query(SourceRow).all():
        total = totals.get(s.id, 0)
        first = first_by_source.get(s.id, 0)
        rows.append(
            SourceQuality(
                source_id=s.id,
                source_name=s.name,
                category=s.category,
                prestige_weight=s.prestige_weight,
                total_mentions=total,
                distinct_makers=distincts.get(s.id, 0),
                first_surfaced=first,
                first_ratio=(first / total) if total else 0.0,
            )
        )

    rows.sort(key=lambda r: (-r.total_mentions, r.source_name))
    return rows
