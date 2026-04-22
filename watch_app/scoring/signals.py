"""Signal calculators — each function computes one signal for one maker.

A signal is a float value describing some property of a maker: how many times
they've been mentioned, how many distinct sources have covered them, how
recent their latest mention is, etc.

Signals are written to the `signals` table historically (one row per
computation), so the ranker can pull the most recent value per maker and
trends can be shown over time.

Phase 0 ships one signal: mention_count. Phase 1 will add:
- source_diversity (count of distinct sources)
- velocity_90d (mentions in last 90 days vs. prior 90)
- prestige_weighted_mentions (sum of source prestige weights)
- first_seen_recency (days since first mention — newer = more "emerging")
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from watch_app.storage.models import Maker, Mention, Signal


def compute_mention_count(session: Session, maker: Maker) -> float:
    """Total count of mentions for this maker, across all sources and time."""
    count = (
        session.query(func.count(Mention.id))
        .filter(Mention.maker_id == maker.id)
        .scalar()
    )
    return float(count or 0)


#: Registry of signal_type -> calculator function. Phase 1 adds more entries.
SIGNAL_CALCULATORS = {
    "mention_count": compute_mention_count,
}


def compute_and_store_signals(session: Session, maker: Maker) -> dict[str, float]:
    """Compute every registered signal for a maker and persist them.

    Returns the dict of signal_type -> value for immediate use by the ranker,
    so the caller doesn't have to re-query the DB.
    """
    now = datetime.now(timezone.utc)
    computed: dict[str, float] = {}

    for signal_type, calculator in SIGNAL_CALCULATORS.items():
        value = calculator(session, maker)
        computed[signal_type] = value
        session.add(
            Signal(
                maker_id=maker.id,
                signal_type=signal_type,
                value=value,
                computed_at=now,
            )
        )

    return computed
