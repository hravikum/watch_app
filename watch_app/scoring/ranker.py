"""Ranker — turn per-maker signals into composite Scores under a preset.

Flow:
    For each maker:
        compute all signals (delegated to scoring.signals)
    Optionally normalize each signal via z-score across the maker population.
    For each maker:
        composite = sum(signal_value * preset_weight for each weighted signal)
        if preset.independent_multiplier:
            composite *= signal["independent_flag"]   (0 for non-indies → dropped)
    Sort by composite, assign ranks, write Score rows.

Normalization matters because raw signals have wildly different ranges:
mention_count can be 50, source_diversity maxes around 5, first_seen_recency
is already [0,1]. Without normalization, weights are fiction — whichever
signal has the largest absolute range dominates. Z-scoring rescales each
signal to mean 0, stddev 1 across the maker population, making weights
actually comparable.

Z-scoring is default-on. Pass `normalize=False` to skip it for debugging
or intentional raw behavior.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from watch_app.scoring.presets import Preset, get_preset
from watch_app.scoring.signals import compute_and_store_signals
from watch_app.storage.models import Maker, Score


def _zscore(values: list[float]) -> list[float]:
    """Return the z-scored version of a list of values.

    Mean 0, stddev 1. If all values are identical, returns a list of zeros
    (avoids division by zero).
    """
    if not values:
        return []
    mean = statistics.mean(values)
    # Population stddev (pstdev), not sample — we're describing a fixed
    # population (all currently-scored makers), not sampling.
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return [0.0] * len(values)
    return [(v - mean) / stdev for v in values]


def _normalize_signals(
    signals_by_maker: dict[int, dict[str, float]],
) -> dict[int, dict[str, float]]:
    """Z-score normalize each signal across the maker population.

    `independent_flag` is excluded from normalization — it's a binary flag,
    not a continuous signal, and normalizing it would break the multiplier
    semantics.
    """
    if not signals_by_maker:
        return signals_by_maker

    # Collect the set of signal names (they should all be the same across
    # makers since SIGNAL_CALCULATORS is deterministic, but defensively union).
    all_signal_names: set[str] = set()
    for sigs in signals_by_maker.values():
        all_signal_names.update(sigs.keys())

    # Stable order so tests are deterministic.
    signal_names = sorted(all_signal_names)
    maker_ids = list(signals_by_maker.keys())

    normalized: dict[int, dict[str, float]] = {mid: {} for mid in maker_ids}

    for signal_name in signal_names:
        # Preserve binary flags unchanged.
        if signal_name == "independent_flag":
            for mid in maker_ids:
                normalized[mid][signal_name] = signals_by_maker[mid].get(
                    signal_name, 0.0
                )
            continue

        raw_values = [
            signals_by_maker[mid].get(signal_name, 0.0) for mid in maker_ids
        ]
        z_values = _zscore(raw_values)
        for mid, z in zip(maker_ids, z_values):
            normalized[mid][signal_name] = z

    return normalized


def _composite(signals: dict[str, float], preset: Preset) -> float:
    """Weighted sum over signals, optionally multiplied by independent flag."""
    total = sum(
        value * preset.weights.get(sig, 0.0) for sig, value in signals.items()
    )
    if preset.independent_multiplier:
        total *= signals.get("independent_flag", 1.0)
    return total


def rank_all_makers(
    session: Session,
    preset_name: str,
    *,
    normalize: bool = True,
) -> list[Score]:
    """Compute signals + scores for every Maker under the given preset.

    Returns the list of newly-created Score objects, sorted by rank (best first).
    Persists Signals and Scores to the DB.
    """
    preset = get_preset(preset_name)
    now = datetime.now(timezone.utc)

    makers = session.query(Maker).all()

    # First pass: compute raw signals for every maker.
    signals_by_maker: dict[int, dict[str, float]] = {}
    for maker in makers:
        signals_by_maker[maker.id] = compute_and_store_signals(session, maker)
        maker.last_scored_at = now

    # Normalize if requested. The independent flag is passed through as-is.
    effective_signals = (
        _normalize_signals(signals_by_maker) if normalize else signals_by_maker
    )

    # Second pass: compute composite score for each maker.
    maker_by_id = {m.id: m for m in makers}
    scored: list[tuple[Maker, float]] = []
    for mid, sigs in effective_signals.items():
        composite = _composite(sigs, preset)
        scored.append((maker_by_id[mid], composite))

    # Rank: highest score = 1. Ties get consecutive ranks — we break arbitrarily
    # by maker id for stability.
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))

    created: list[Score] = []
    for rank_idx, (maker, value) in enumerate(scored, start=1):
        score = Score(
            maker_id=maker.id,
            preset=preset_name,
            value=value,
            rank=rank_idx,
            computed_at=now,
        )
        session.add(score)
        created.append(score)

    return created
