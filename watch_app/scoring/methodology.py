"""Helpers for the /methodology page.

The page needs two categories of content:

1. Static explanations of each signal, its formula, and its intent.
   Lives in SIGNAL_DESCRIPTIONS below, alongside the code it describes.

2. A live worked example for each preset that walks through the math for
   the currently top-ranked maker, using real data. This requires
   reconstructing the z-score normalization the ranker performs at scan
   time — which is deterministic given the same inputs, so we just rerun
   it at render time.

Separating the presentation logic from the ranker keeps the ranker focused
on persistence, and lets the methodology page show intermediate steps
(raw values, z-scores, weighted contributions) that aren't themselves
stored in the DB.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from watch_app.scoring.presets import PRESETS, Preset
from watch_app.storage.models import Maker, Signal


#: Human-readable description and formula for each signal. The keys here MUST
#: match the signal_type strings produced by signals.compute_and_store_signals.
#: If you add a new signal, add its entry here — the methodology template
#: iterates this dict.
SIGNAL_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "mention_count": {
        "label": "Mention count",
        "formula": "count(mentions for maker)",
        "description": (
            "Total number of recorded mentions across every source. "
            "The most basic popularity signal."
        ),
    },
    "prestige_weighted_mentions": {
        "label": "Prestige-weighted mentions",
        "formula": "Σ media-mention prestige + Σ distinct award-source prestige",
        "description": (
            "Mentions weighted by source prestige (GPHG 1.2, SJX 1.0, "
            "Monochrome 0.9, Quill & Pad 0.8, Hodinkee/Fratello 0.7). "
            "Award-source mentions are capped at one per source: a brand "
            "with 14 GPHG nominee watches doesn't get 14× the prestige "
            "of a brand with one — that conflates operational scale with "
            "rising-indie signal. Media mentions are uncapped: 4 SJX "
            "articles is genuinely 4× the press signal of 1."
        ),
    },
    "source_diversity": {
        "label": "Source diversity",
        "formula": "count(distinct sources mentioning maker)",
        "description": (
            "How many different outlets have covered this maker. Three "
            "mentions across three outlets is a stronger signal than three "
            "mentions from one outlet."
        ),
    },
    "velocity_90d": {
        "label": "Velocity (90-day)",
        "formula": "count(mentions in last 90 days)",
        "description": (
            "Mentions in the recent 90-day window. Intended to catch "
            "makers whose coverage is accelerating right now. Becomes "
            "discriminating over weeks of accumulated scan data."
        ),
    },
    "first_seen_recency": {
        "label": "First-seen recency",
        "formula": "max(0, 1 − days_since_first_mention / 180)",
        "description": (
            "Linear decay from 1.0 (first mention today) to 0 (first "
            "mention 180+ days ago). Rewards makers just emerging into "
            "our data — not newness of the brand itself, but newness of "
            "their appearance in watch media."
        ),
    },
    "independent_flag": {
        "label": "Independent flag",
        "formula": "1 if maker.is_independent else 0",
        "description": (
            "Binary flag from the seed YAML. Zero for conglomerate brands "
            "(Rolex, Hublot, Patek Philippe, etc.), one for independents. "
            "Passed through without z-score normalization — it's a gate, "
            "not a continuous signal."
        ),
    },
    "obscurity": {
        "label": "Obscurity",
        "formula": "1 / (1 + mention_count)",
        "description": (
            "Inverse of mention count. Peaks at 1.0 for zero-mention "
            "makers, declines toward zero as mentions accumulate. The "
            "core of the hidden_gem preset."
        ),
    },
}


@dataclass
class WorkedExampleRow:
    """One row of the worked example table — one signal's contribution."""

    signal_name: str
    signal_label: str
    raw_value: float
    z_value: float | None  # None if passed-through (independent_flag)
    weight: float
    contribution: float  # z_value * weight, or raw_value * weight if not normalized


@dataclass
class WorkedExample:
    """Everything the template needs to render a walked-through calculation."""

    maker_id: int
    maker_name: str
    preset_name: str
    rows: list[WorkedExampleRow]
    weighted_sum: float
    independent_multiplier_applied: bool
    independent_flag_value: float
    final_score: float
    #: If the maker was filtered by max_mentions_for_eligibility, this is set.
    filtered_by_cap: bool
    cap_value: int | None


def _latest_signals_by_maker(session: Session) -> dict[int, dict[str, float]]:
    """Fetch the most recent Signal row per (maker, signal_type).

    Mirrors the "latest per group" pattern used elsewhere — GROUP BY
    (maker_id, signal_type) taking max(computed_at), then join back.

    Returns {maker_id: {signal_type: value}}.
    """
    latest_times = (
        session.query(
            Signal.maker_id,
            Signal.signal_type,
            func.max(Signal.computed_at).label("max_at"),
        )
        .group_by(Signal.maker_id, Signal.signal_type)
        .subquery()
    )
    rows = (
        session.query(Signal)
        .join(
            latest_times,
            (Signal.maker_id == latest_times.c.maker_id)
            & (Signal.signal_type == latest_times.c.signal_type)
            & (Signal.computed_at == latest_times.c.max_at),
        )
        .all()
    )
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r.maker_id, {})[r.signal_type] = r.value
    return out


def _zscore_values(values: list[float]) -> list[float]:
    """Same math as ranker._zscore, duplicated to avoid a circular-ish import
    and to stay independent of the ranker's internals.
    """
    if not values:
        return []
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return [0.0] * len(values)
    return [(v - mean) / stdev for v in values]


def _normalize(
    signals_by_maker: dict[int, dict[str, float]],
) -> dict[int, dict[str, float]]:
    """Z-score normalize each signal, passing `independent_flag` through.

    Mirrors ranker._normalize_signals.
    """
    if not signals_by_maker:
        return {}

    all_names: set[str] = set()
    for sigs in signals_by_maker.values():
        all_names.update(sigs.keys())

    maker_ids = list(signals_by_maker.keys())
    normalized: dict[int, dict[str, float]] = {mid: {} for mid in maker_ids}

    for name in sorted(all_names):
        if name == "independent_flag":
            for mid in maker_ids:
                normalized[mid][name] = signals_by_maker[mid].get(name, 0.0)
            continue
        raw = [signals_by_maker[mid].get(name, 0.0) for mid in maker_ids]
        zs = _zscore_values(raw)
        for mid, z in zip(maker_ids, zs):
            normalized[mid][name] = z

    return normalized


def build_worked_example(
    session: Session,
    preset_name: str,
) -> WorkedExample | None:
    """Build a worked example for the #1 maker under the given preset.

    Picks the top-ranked maker (by the freshly-recomputed normalized score)
    and produces a breakdown of how each signal contributes to their total.

    Returns None if there are no makers with signals yet.
    """
    preset: Preset = PRESETS[preset_name]

    signals_by_maker = _latest_signals_by_maker(session)
    if not signals_by_maker:
        return None

    normalized = _normalize(signals_by_maker)

    # Compute the composite for each maker, applying eligibility gates the
    # same way the ranker does.
    cap = preset.max_mentions_for_eligibility
    scored: list[tuple[int, float, bool]] = []  # (maker_id, composite, filtered)
    for mid, sigs in normalized.items():
        raw = signals_by_maker[mid]
        filtered = cap is not None and raw.get("mention_count", 0.0) > cap
        if filtered:
            scored.append((mid, 0.0, True))
            continue
        total = sum(
            v * preset.weights.get(name, 0.0) for name, v in sigs.items()
        )
        if preset.independent_multiplier:
            total *= sigs.get("independent_flag", 1.0)
        scored.append((mid, total, False))

    # Pick the top non-zero-score maker. If all are filtered/zero (very early
    # state, no data), pick whichever has the highest raw score anyway so the
    # user sees SOME example rather than nothing.
    scored.sort(key=lambda t: (-t[1], t[0]))
    top_id, top_total, top_filtered = scored[0]

    maker = session.get(Maker, top_id)
    if maker is None:  # Paranoia — signals exist but maker was deleted.
        return None

    raw_sigs = signals_by_maker[top_id]
    norm_sigs = normalized[top_id]

    rows: list[WorkedExampleRow] = []
    for name in SIGNAL_DESCRIPTIONS:
        weight = preset.weights.get(name, 0.0)
        raw_val = raw_sigs.get(name, 0.0)
        if name == "independent_flag":
            z_val: float | None = None  # Pass-through; z-score not applicable
            contribution = raw_val * weight
        else:
            z_val = norm_sigs.get(name, 0.0)
            contribution = z_val * weight
        rows.append(
            WorkedExampleRow(
                signal_name=name,
                signal_label=SIGNAL_DESCRIPTIONS[name]["label"],
                raw_value=raw_val,
                z_value=z_val,
                weight=weight,
                contribution=contribution,
            )
        )

    weighted_sum = sum(r.contribution for r in rows if r.signal_name != "independent_flag")
    # Add the flag's own weighted contribution to weighted_sum only if the
    # preset actually weights it (rare; normally used via the multiplier).
    weighted_sum += sum(
        r.contribution for r in rows if r.signal_name == "independent_flag"
    )

    return WorkedExample(
        maker_id=maker.id,
        maker_name=maker.canonical_name,
        preset_name=preset_name,
        rows=rows,
        weighted_sum=weighted_sum,
        independent_multiplier_applied=preset.independent_multiplier,
        independent_flag_value=raw_sigs.get("independent_flag", 1.0),
        final_score=top_total,
        filtered_by_cap=top_filtered,
        cap_value=cap,
    )


# --- Source contributions (added 2H) ---

#: Time window for "recent" activity stats (mentions in last N days).
RECENT_WINDOW_DAYS = 7


@dataclass
class SourceContribution:
    """Per-source statistics for the source-auditing table.

    Tells you what each ingested source has actually contributed to the
    population. The intent is for this to make calibration decisions
    visible — e.g., "Hodinkee has 47 lifetime mentions but only 8 distinct
    makers, so it's mostly re-covering the same brands" or "Quill & Pad
    surfaced 5 makers no other source has, despite a lower prestige weight."
    """

    source_name: str
    category: str
    prestige_weight: float
    total_mentions: int
    distinct_makers: int
    first_surfaced_count: int  # makers whose first-ever mention came from this source
    mentions_recent: int  # mentions in last RECENT_WINDOW_DAYS days
    source_id: int = 0  # for linking to the per-source detail page (3M)


def build_source_contributions(session: Session) -> list[SourceContribution]:
    """Compute the source-auditing table.

    Returns one row per source, ordered by total_mentions descending. Sources
    with zero mentions are still included — useful to see if a configured
    source has gone silent.

    "First-surfaced" is computed as: for each maker, find the source that
    produced their earliest mention by `discovered_at`. Then count, per
    source, how many makers it was the first to surface. This rewards
    sources that genuinely do discovery work rather than just re-covering
    what's already been seen elsewhere.

    When multiple sources have mentions at the same earliest timestamp
    (the test population can do this; in production it occurs when GPHG
    editions are ingested in the same scan), ties are broken by picking
    the lowest source_id — deterministic, doesn't matter much since real
    timestamps usually differ by milliseconds. The total first_surfaced
    across all sources always equals the total maker count.

    Note that GPHG has a head start (we ingested 3 editions on day 1) so
    it will dominate this column at first; that's accurate, not a bug.
    """
    from watch_app.storage.models import Mention, Source as SourceRow

    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)

    # Per-maker first-surfacing source. Subquery: min(discovered_at) per
    # maker_id; outer query: join back to find which source had that timestamp.
    # Tie-break: when multiple sources have a mention at the same earliest
    # timestamp, we attribute the surface to MIN(source_id) — deterministic
    # and stable across runs. Without this, ties would cause the same maker
    # to be counted for multiple sources, inflating first_surfaced totals
    # beyond the actual maker count.
    earliest_per_maker = (
        session.query(
            Mention.maker_id,
            func.min(Mention.discovered_at).label("first_at"),
        )
        .group_by(Mention.maker_id)
        .subquery()
    )
    # For each (maker_id, first_at), pick the lowest source_id among
    # mentions matching that timestamp.
    surfacing_source = (
        session.query(
            Mention.maker_id,
            func.min(Mention.source_id).label("source_id"),
        )
        .join(
            earliest_per_maker,
            (Mention.maker_id == earliest_per_maker.c.maker_id)
            & (Mention.discovered_at == earliest_per_maker.c.first_at),
        )
        .group_by(Mention.maker_id)
        .subquery()
    )
    first_source_rows = (
        session.query(
            surfacing_source.c.source_id,
            func.count(surfacing_source.c.maker_id).label("count"),
        )
        .group_by(surfacing_source.c.source_id)
        .all()
    )
    first_surfaced_by_source: dict[int, int] = {
        sid: count for sid, count in first_source_rows
    }

    # Total mentions per source.
    totals = dict(
        session.query(Mention.source_id, func.count(Mention.id))
        .group_by(Mention.source_id)
        .all()
    )

    # Distinct makers per source.
    distincts = dict(
        session.query(Mention.source_id, func.count(func.distinct(Mention.maker_id)))
        .group_by(Mention.source_id)
        .all()
    )

    # Mentions in the recent window per source.
    recents = dict(
        session.query(Mention.source_id, func.count(Mention.id))
        .filter(Mention.discovered_at >= cutoff)
        .group_by(Mention.source_id)
        .all()
    )

    sources = session.query(SourceRow).all()
    rows: list[SourceContribution] = []
    for s in sources:
        rows.append(
            SourceContribution(
                source_name=s.name,
                category=s.category,
                prestige_weight=s.prestige_weight,
                total_mentions=totals.get(s.id, 0),
                distinct_makers=distincts.get(s.id, 0),
                first_surfaced_count=first_surfaced_by_source.get(s.id, 0),
                mentions_recent=recents.get(s.id, 0),
                source_id=s.id,
            )
        )

    # Sort by total mentions descending; ties broken by source name for stability.
    rows.sort(key=lambda r: (-r.total_mentions, r.source_name))
    return rows


# --- Per-source detail (chunk 3M) ---


@dataclass
class SourceDetail:
    """Everything the /sources/{id} drill-down needs for one source.

    Reuses the same first-surfaced logic as build_source_contributions, scoped
    to a single source and expanded into the actual maker lists.
    """

    source_id: int
    source_name: str
    category: str
    url: str | None
    prestige_weight: float
    active: bool
    last_scraped_at: datetime | None
    total_mentions: int
    distinct_makers: int
    first_surfaced_count: int
    mentions_recent: int
    first_surfaced: list[dict]   # {maker_id, name}
    top_makers: list[dict]       # {maker_id, name, mention_count}
    recent_mentions: list[dict]  # {title, url, when, maker_id, maker_name}


def build_source_detail(
    session: Session, source_id: int, *, top_n: int = 10, recent_n: int = 12
) -> SourceDetail | None:
    """Build the per-source profile, or None if the source doesn't exist."""
    from watch_app.storage.models import Maker, Mention, Source as SourceRow

    source = session.get(SourceRow, source_id)
    if source is None:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)
    by_source = Mention.source_id == source_id

    total_mentions = (
        session.query(func.count(Mention.id)).filter(by_source).scalar() or 0
    )
    distinct_makers = (
        session.query(func.count(func.distinct(Mention.maker_id)))
        .filter(by_source)
        .scalar()
        or 0
    )
    mentions_recent = (
        session.query(func.count(Mention.id))
        .filter(by_source, Mention.discovered_at >= cutoff)
        .scalar()
        or 0
    )

    # First-surfaced makers credited to this source (same tie-break as the
    # contributions table: earliest discovered_at per maker, MIN(source_id) on
    # ties), expanded to the maker list rather than just a count.
    earliest_per_maker = (
        session.query(
            Mention.maker_id,
            func.min(Mention.discovered_at).label("first_at"),
        )
        .group_by(Mention.maker_id)
        .subquery()
    )
    surfacing_source = (
        session.query(
            Mention.maker_id,
            func.min(Mention.source_id).label("source_id"),
        )
        .join(
            earliest_per_maker,
            (Mention.maker_id == earliest_per_maker.c.maker_id)
            & (Mention.discovered_at == earliest_per_maker.c.first_at),
        )
        .group_by(Mention.maker_id)
        .subquery()
    )
    first_rows = (
        session.query(surfacing_source.c.maker_id, Maker.canonical_name)
        .join(Maker, Maker.id == surfacing_source.c.maker_id)
        .filter(surfacing_source.c.source_id == source_id)
        .order_by(Maker.canonical_name)
        .all()
    )
    first_surfaced = [{"maker_id": mid, "name": name} for mid, name in first_rows]

    top_rows = (
        session.query(
            Mention.maker_id,
            Maker.canonical_name,
            func.count(Mention.id).label("c"),
        )
        .join(Maker, Maker.id == Mention.maker_id)
        .filter(by_source)
        .group_by(Mention.maker_id, Maker.canonical_name)
        .order_by(func.count(Mention.id).desc(), Maker.canonical_name)
        .limit(top_n)
        .all()
    )
    top_makers = [
        {"maker_id": mid, "name": name, "mention_count": c}
        for mid, name, c in top_rows
    ]

    recent_rows = (
        session.query(
            Mention.title,
            Mention.url,
            Mention.discovered_at,
            Mention.maker_id,
            Maker.canonical_name,
        )
        .join(Maker, Maker.id == Mention.maker_id)
        .filter(by_source)
        .order_by(Mention.discovered_at.desc())
        .limit(recent_n)
        .all()
    )
    recent_mentions = [
        {
            "title": title,
            "url": url,
            "when": when,
            "maker_id": mid,
            "maker_name": mname,
        }
        for title, url, when, mid, mname in recent_rows
    ]

    return SourceDetail(
        source_id=source.id,
        source_name=source.name,
        category=source.category,
        url=source.url,
        prestige_weight=source.prestige_weight,
        active=source.active,
        last_scraped_at=source.last_scraped_at,
        total_mentions=total_mentions,
        distinct_makers=distinct_makers,
        first_surfaced_count=len(first_surfaced),
        mentions_recent=mentions_recent,
        first_surfaced=first_surfaced,
        top_makers=top_makers,
        recent_mentions=recent_mentions,
    )
