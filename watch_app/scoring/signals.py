"""Signal calculators.

Each calculator takes (session, maker) and returns a float. The ranker then
combines signal values with preset weights to produce a composite score.

All signals are kept historically in the `signals` table — one row per
(maker, signal_type, computed_at) — so we can show trends over time.

Philosophy:
- Signals are raw, interpretable numbers. "7 mentions", "4 distinct sources",
  "21 days since first mention". Normalization happens in the ranker, not here.
- If a maker lacks the data for a signal (e.g. no mentions yet), the signal
  should return a sensible zero or minimum value, not raise.
- Adding a new signal is just adding another function + entry in
  SIGNAL_CALCULATORS. The ranker picks it up automatically if the preset
  gives it a weight.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from watch_app.storage.models import Maker, Mention, Signal
from watch_app.storage.models import Source as SourceRow


#: Window for the velocity signal. Mentions older than this don't count.
VELOCITY_WINDOW_DAYS = 90

#: Cap for the first_seen_recency signal — mentions older than this contribute
#: the minimum (0.0). Prevents long-quiet makers from looking "emerging"
#: forever.
RECENCY_CAP_DAYS = 180


def compute_mention_count(session: Session, maker: Maker) -> float:
    """Total count of mentions, across all sources and all time."""
    count = (
        session.query(func.count(Mention.id))
        .filter(Mention.maker_id == maker.id)
        .scalar()
    )
    return float(count or 0)


def compute_prestige_weighted_mentions(session: Session, maker: Maker) -> float:
    """Sum of source.prestige_weight for every mention of this maker, with
    award-source mentions capped at one per source row.

    A mention on SJX (prestige 1.0) is worth more than one on Hodinkee (0.7).

    Award sources (GPHG 2023/2024/2025) are capped: a brand that submitted 14
    watches across 3 GPHG editions doesn't get 14× the prestige of a brand
    that submitted 1 watch — that would conflate "operational scale" with
    "rising indie." The cap counts each award-source contribution once,
    regardless of how many of that brand's watches it nominated.

    Media sources are unbounded: 4 SJX articles is genuinely 4× the press
    signal of 1 article, since each is a discrete editorial decision.
    """
    # Sum across all sources, but for awards each source contributes its
    # weight at most once (regardless of mention count).
    awards_contribution = (
        session.query(func.coalesce(func.sum(SourceRow.prestige_weight), 0.0))
        .filter(SourceRow.category == "awards")
        .filter(
            SourceRow.id.in_(
                session.query(Mention.source_id)
                .filter(Mention.maker_id == maker.id)
                .distinct()
            )
        )
        .scalar()
    ) or 0.0

    media_contribution = (
        session.query(func.coalesce(func.sum(SourceRow.prestige_weight), 0.0))
        .join(Mention, Mention.source_id == SourceRow.id)
        .filter(Mention.maker_id == maker.id)
        .filter(SourceRow.category != "awards")
        .scalar()
    ) or 0.0

    return float(awards_contribution + media_contribution)


def compute_source_diversity(session: Session, maker: Maker) -> float:
    """Count of *distinct* sources that have mentioned this maker.

    Covered by 3 different outlets is a stronger signal than 10 hits in one.
    """
    count = (
        session.query(func.count(func.distinct(Mention.source_id)))
        .filter(Mention.maker_id == maker.id)
        .scalar()
    )
    return float(count or 0)


def compute_velocity_90d(session: Session, maker: Maker) -> float:
    """Count of mentions in the trailing 90 days.

    Uses Mention.published_at when available, falling back to discovered_at.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=VELOCITY_WINDOW_DAYS)
    effective_date = func.coalesce(Mention.published_at, Mention.discovered_at)
    count = (
        session.query(func.count(Mention.id))
        .filter(Mention.maker_id == maker.id)
        .filter(effective_date >= cutoff)
        .scalar()
    )
    return float(count or 0)


def compute_first_seen_recency(session: Session, maker: Maker) -> float:
    """Normalized recency of the earliest mention for this maker.

    Returns 1.0 if the earliest mention is today, declining linearly to 0.0
    at RECENCY_CAP_DAYS. Past the cap, returns 0.0. Returns 0.0 if the maker
    has no mentions at all (no evidence of emergence yet).

    Uses first-*mention* date, not Maker.first_seen_at — the latter is set
    when the seed loader writes a maker to the DB, which is a different
    concept (it means "we've been tracking them since...") and would make
    every seed maker look equally "old" from day one.
    """
    earliest = (
        session.query(
            func.min(func.coalesce(Mention.published_at, Mention.discovered_at))
        )
        .filter(Mention.maker_id == maker.id)
        .scalar()
    )
    if earliest is None:
        return 0.0

    # Normalize naive datetimes (SQLite) to UTC — our "now" is tz-aware.
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    days = (now - earliest).total_seconds() / 86400.0
    if days <= 0:
        return 1.0
    if days >= RECENCY_CAP_DAYS:
        return 0.0
    return 1.0 - (days / RECENCY_CAP_DAYS)


def compute_independent_flag(session: Session, maker: Maker) -> float:
    """1.0 if the maker is flagged as independent, 0.0 otherwise.

    Used as a zero-multiplier in `rising_star` to exclude conglomerate
    brands from that preset. Ignored by `hidden_gem` (which gives it
    weight 0 in the preset definition).
    """
    return 1.0 if maker.is_independent else 0.0


def compute_obscurity(session: Session, maker: Maker) -> float:
    """Inverse-of-mentions signal. Peaks at 1.0 for zero mentions.

    Formula: 1 / (1 + mention_count). So:
      0 mentions → 1.000  (max obscurity — truly hidden)
      1 mention  → 0.500
      3 mentions → 0.250
      10 mentions → 0.091
      50 mentions → 0.020

    Used primarily by `hidden_gem`. The decay shape means makers with
    low-but-nonzero mentions still rank meaningfully above heavily-covered
    ones, but a zero-mention maker gets a decisive boost.

    We use this instead of negatively weighting `mention_count` directly
    because, under z-score normalization, a raw negative weight on
    mention_count loses the semantics — the zero-mention makers end up
    only moderately rewarded rather than decisively.
    """
    count = compute_mention_count(session, maker)
    return 1.0 / (1.0 + count)


#: Registry of signal_type -> calculator. Add new entries here; the ranker
#: picks them up automatically when a preset gives them a weight.
SIGNAL_CALCULATORS = {
    "mention_count": compute_mention_count,
    "prestige_weighted_mentions": compute_prestige_weighted_mentions,
    "source_diversity": compute_source_diversity,
    "velocity_90d": compute_velocity_90d,
    "first_seen_recency": compute_first_seen_recency,
    "independent_flag": compute_independent_flag,
    "obscurity": compute_obscurity,
}


def compute_and_store_signals(session: Session, maker: Maker) -> dict[str, float]:
    """Compute every registered signal for a maker and persist them.

    Returns the dict of signal_type -> value for immediate use by the ranker.
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
