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
        "formula": "Σ (source.prestige_weight) over mentions",
        "description": (
            "Mention count weighted by how prestigious the source is. GPHG "
            "(1.2), SJX (1.0), and Monochrome (0.9) contribute more than "
            "Hodinkee (0.7) or Fratello (0.7). A single GPHG nomination "
            "outweighs a single Fratello article by design."
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
