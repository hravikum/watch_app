"""Shared scoring queries.

Centralizes the "latest score per maker" lookup so the CLI and the web UI
use the same logic. Previously each had its own copy and both were broken.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from watch_app.storage.models import Maker, Mention, Score


def latest_scores_query(preset_name: str):
    """Return a selectable that yields the latest Score per maker under the preset.

    Uses a correlated subquery to find max(computed_at) per maker_id, then joins
    Score back on (maker_id, max_computed_at). Produces exactly one row per maker
    with a score in this preset.
    """
    # Inner: find the latest computed_at per maker for this preset.
    latest = (
        select(
            Score.maker_id,
            func.max(Score.computed_at).label("max_computed_at"),
        )
        .where(Score.preset == preset_name)
        .group_by(Score.maker_id)
        .subquery()
    )

    # Outer: join Score on that to get the actual value/rank from that row.
    s = aliased(Score)
    stmt = (
        select(s)
        .join(
            latest,
            (s.maker_id == latest.c.maker_id)
            & (s.computed_at == latest.c.max_computed_at),
        )
        .where(s.preset == preset_name)
    )
    return stmt, s


def fetch_top_makers(
    session: Session,
    preset_name: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return ranked top makers under `preset_name`.

    Each dict has: rank, maker_id, canonical_name, country, score, mention_count.
    The list is ordered by the latest-score rank (best first).
    """
    stmt, score_alias = latest_scores_query(preset_name)
    stmt = stmt.order_by(score_alias.rank.asc()).limit(limit)

    score_rows = session.execute(stmt).scalars().all()
    if not score_rows:
        return []

    # Pull makers + mention counts in one go, keyed by id for assembly.
    maker_ids = [s.maker_id for s in score_rows]
    makers_by_id = {
        m.id: m
        for m in session.query(Maker).filter(Maker.id.in_(maker_ids)).all()
    }
    mention_counts = dict(
        session.query(Mention.maker_id, func.count(Mention.id))
        .filter(Mention.maker_id.in_(maker_ids))
        .group_by(Mention.maker_id)
        .all()
    )

    result: list[dict[str, Any]] = []
    for s in score_rows:
        maker = makers_by_id.get(s.maker_id)
        if maker is None:
            continue
        result.append(
            {
                "rank": s.rank,
                "maker_id": maker.id,
                "canonical_name": maker.canonical_name,
                "country": maker.country,
                "score": s.value,
                "mention_count": mention_counts.get(maker.id, 0),
            }
        )
    return result


def fetch_all_makers_comparison(
    session: Session,
    preset_a: str,
    preset_b: str,
) -> list[dict[str, Any]]:
    """Return every maker that has a latest score under either preset, with
    both preset scores and ranks side by side.

    Used by the /comparison page. Makers with scores in only one preset
    still appear; their missing-preset rank/score will be None. The
    `divergence` field is the absolute difference between the two ranks
    when both are present; None otherwise. Sorted by maker_id ascending
    (template handles display ordering).
    """
    stmt_a, score_a = latest_scores_query(preset_a)
    stmt_b, score_b = latest_scores_query(preset_b)

    rows_a = session.execute(stmt_a).scalars().all()
    rows_b = session.execute(stmt_b).scalars().all()

    by_maker_a = {s.maker_id: s for s in rows_a}
    by_maker_b = {s.maker_id: s for s in rows_b}
    all_maker_ids = sorted(set(by_maker_a) | set(by_maker_b))
    if not all_maker_ids:
        return []

    makers_by_id = {
        m.id: m
        for m in session.query(Maker).filter(Maker.id.in_(all_maker_ids)).all()
    }
    mention_counts = dict(
        session.query(Mention.maker_id, func.count(Mention.id))
        .filter(Mention.maker_id.in_(all_maker_ids))
        .group_by(Mention.maker_id)
        .all()
    )

    out: list[dict[str, Any]] = []
    for mid in all_maker_ids:
        maker = makers_by_id.get(mid)
        if maker is None:
            continue
        sa = by_maker_a.get(mid)
        sb = by_maker_b.get(mid)
        rank_a = sa.rank if sa else None
        rank_b = sb.rank if sb else None
        divergence = (
            abs(rank_a - rank_b) if rank_a is not None and rank_b is not None
            else None
        )
        out.append(
            {
                "maker_id": maker.id,
                "canonical_name": maker.canonical_name,
                "country": maker.country,
                "is_independent": maker.is_independent,
                "mention_count": mention_counts.get(maker.id, 0),
                "rank_a": rank_a,
                "score_a": sa.value if sa else None,
                "rank_b": rank_b,
                "score_b": sb.value if sb else None,
                "divergence": divergence,
            }
        )
    return out


def fetch_rank_changes(
    session: Session,
    preset_name: str,
    days: int = 7,
    min_change: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Compute week-over-week rank changes for a preset.

    Returns three lists of makers under keys 'climbers', 'fallers', and
    'new_entrants':

    - climbers: makers whose rank improved by >= min_change positions
    - fallers: makers whose rank worsened by >= min_change positions
    - new_entrants: makers ranked today but not `days` ago (either truly
      new in the DB, or makers who moved off the floor onto the
      scoreboard for the first time)

    Climbers and fallers are sorted by absolute rank_change desc.
    New entrants are sorted by current rank ascending — the most
    promising new arrivals first.

    The `days` window is clamped to a reasonable range (1-30) to avoid
    pathological queries; 7 is the default since week-over-week is the
    natural cadence for watch media.
    """
    from datetime import datetime, timedelta, timezone

    days = max(1, min(30, days))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Latest rank per maker for this preset.
    latest_stmt, latest_alias = latest_scores_query(preset_name)
    latest_rows = session.execute(latest_stmt).scalars().all()
    if not latest_rows:
        return {"climbers": [], "fallers": [], "new_entrants": []}

    latest_by_maker = {s.maker_id: s for s in latest_rows}

    # Rank N days ago: for each maker, the most recent score whose
    # computed_at is <= cutoff. We use the same correlated-subquery shape
    # as latest_scores_query but with the cutoff constraint.
    historical_subq = (
        select(
            Score.maker_id,
            func.max(Score.computed_at).label("max_at"),
        )
        .where(Score.preset == preset_name)
        .where(Score.computed_at <= cutoff)
        .group_by(Score.maker_id)
        .subquery()
    )
    h = aliased(Score)
    historical_stmt = (
        select(h)
        .join(
            historical_subq,
            (h.maker_id == historical_subq.c.maker_id)
            & (h.computed_at == historical_subq.c.max_at),
        )
        .where(h.preset == preset_name)
    )
    historical_rows = session.execute(historical_stmt).scalars().all()
    historical_by_maker = {s.maker_id: s for s in historical_rows}

    # Hydrate makers and mention counts in batch.
    all_ids = list(latest_by_maker.keys())
    makers_by_id = {
        m.id: m
        for m in session.query(Maker).filter(Maker.id.in_(all_ids)).all()
    }
    mention_counts = dict(
        session.query(Mention.maker_id, func.count(Mention.id))
        .filter(Mention.maker_id.in_(all_ids))
        .group_by(Mention.maker_id)
        .all()
    )

    climbers: list[dict[str, Any]] = []
    fallers: list[dict[str, Any]] = []
    new_entrants: list[dict[str, Any]] = []

    for maker_id, latest in latest_by_maker.items():
        maker = makers_by_id.get(maker_id)
        if maker is None:
            continue

        base = {
            "maker_id": maker_id,
            "canonical_name": maker.canonical_name,
            "country": maker.country,
            "is_independent": maker.is_independent,
            "current_rank": latest.rank,
            "current_score": latest.value,
            "mention_count": mention_counts.get(maker_id, 0),
        }

        prior = historical_by_maker.get(maker_id)
        if prior is None:
            # No score on or before the cutoff — this maker entered the
            # rankings sometime in the last `days` days.
            new_entrants.append(
                {**base, "prior_rank": None, "rank_change": None}
            )
            continue

        rank_change = prior.rank - latest.rank  # positive = climbed
        row = {
            **base,
            "prior_rank": prior.rank,
            "prior_score": prior.value,
            "rank_change": rank_change,
        }
        if rank_change >= min_change:
            climbers.append(row)
        elif rank_change <= -min_change:
            fallers.append(row)

    climbers.sort(key=lambda r: -r["rank_change"])
    fallers.sort(key=lambda r: r["rank_change"])  # most negative first
    new_entrants.sort(key=lambda r: r["current_rank"])

    return {
        "climbers": climbers,
        "fallers": fallers,
        "new_entrants": new_entrants,
    }
