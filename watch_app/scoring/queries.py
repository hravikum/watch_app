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
