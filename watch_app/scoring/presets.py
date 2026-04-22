"""Scoring presets — different weight profiles for different hunting modes.

A preset is a mapping from signal type -> weight. The ranker takes the raw
signals for a maker, multiplies by the preset's weights, sums, and that's
the composite score.

Two presets are defined here:

- rising_star: favors makers with growing momentum. Mentions matter a lot,
  source diversity matters, recency matters. Established indies with steady
  coverage rank high.

- hidden_gem: the opposite. Rewards makers with few but high-prestige
  mentions. Penalizes high total volume. We're looking for makers who are
  barely on anyone's radar but the right people are starting to notice.

Phase 0 has one signal (mention_count), so both presets are trivial. The
structure is in place so Phase 1 can add signals like source_diversity,
velocity_90d, prestige_weighted_mentions, etc. without touching the ranker.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    """A named set of signal weights."""

    name: str
    weights: dict[str, float]


PRESETS: dict[str, Preset] = {
    "rising_star": Preset(
        name="rising_star",
        weights={
            "mention_count": 1.0,
            # Phase 1 will add: source_diversity, velocity_90d, prestige_weighted
        },
    ),
    "hidden_gem": Preset(
        name="hidden_gem",
        weights={
            # Negative weight on raw volume — we want obscure.
            "mention_count": -0.3,
            # Phase 1 will add: prestige_weighted (positive), first_seen_recency (positive)
        },
    ),
}


def get_preset(name: str) -> Preset:
    """Return a preset by name. Raises KeyError if unknown."""
    if name not in PRESETS:
        known = ", ".join(sorted(PRESETS.keys()))
        raise KeyError(f"Unknown preset {name!r}. Known presets: {known}")
    return PRESETS[name]
