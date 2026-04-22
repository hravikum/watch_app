"""Ranker — turn per-maker signals into composite Scores under a preset.

Score = sum(signal_value * preset_weight for each signal).

Ranks are assigned within a single computation — the highest-scoring maker
gets rank=1, etc. Historical Score rows are preserved so the UI can show
"this maker moved from rank 47 to rank 12" later.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from watch_app.scoring.presets import Preset, get_preset
from watch_app.scoring.signals import compute_and_store_signals
from watch_app.storage.models import Maker, Score


def _composite(signals: dict[str, float], preset: Preset) -> float:
    """Weighted sum. Signals not in the preset's weights contribute zero."""
    return sum(value * preset.weights.get(sig, 0.0) for sig, value in signals.items())


def rank_all_makers(session: Session, preset_name: str) -> list[Score]:
    """Compute signals + scores for every Maker under the given preset.

    Returns the list of newly-created Score objects, sorted by rank (best first).
    Persists both Signals and Scores to the DB.
    """
    preset = get_preset(preset_name)
    now = datetime.now(timezone.utc)

    makers = session.query(Maker).all()

    # First pass: compute signals + composite score for every maker.
    scored: list[tuple[Maker, float]] = []
    for maker in makers:
        signals = compute_and_store_signals(session, maker)
        composite = _composite(signals, preset)
        scored.append((maker, composite))
        maker.last_scored_at = now

    # Second pass: assign ranks (highest score = rank 1).
    scored.sort(key=lambda pair: pair[1], reverse=True)

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
