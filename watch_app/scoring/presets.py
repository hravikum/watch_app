"""Scoring presets — weight profiles for different hunting modes.

A preset defines how much each signal contributes to the composite score
for a maker.

Two presets now defined:

- rising_star: designed to catch emerging makers with real current
  traction. Heavy weight on source diversity and prestige; emergence
  recency is bumped up so brand-new makers surface faster. Conglomerate
  brands (is_independent=false) are zeroed out via the
  independent_multiplier flag — they can't be "rising stars" in the
  LV-Prize sense.

- hidden_gem: surface makers barely on anyone's radar. Negative weight
  on raw mention count, positive weight on recency and prestige
  (a quiet nod from a high-prestige outlet is the exact signal).
  Conglomerates stay in the pool because sometimes a surprise watch
  from a big house is genuinely under-covered — but with mention-count
  penalties, they typically won't rank anyway.

Signal weights are matched by key against the signals produced by
watch_app.scoring.signals.SIGNAL_CALCULATORS. Signals without a weight
contribute 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Preset:
    """A named set of signal weights and behavior flags."""

    name: str
    weights: dict[str, float] = field(default_factory=dict)

    #: If True, the final score is multiplied by the `independent_flag` signal.
    #: A non-independent maker (flag=0.0) gets a zero score and drops out of the
    #: ranking entirely. Only rising_star uses this; hidden_gem leaves it off.
    independent_multiplier: bool = False


PRESETS: dict[str, Preset] = {
    "rising_star": Preset(
        name="rising_star",
        weights={
            # De-emphasize raw volume — Hodinkee flooding shouldn't dominate.
            "mention_count": 0.2,
            # A mention on a prestige outlet is worth more than one on a
            # lower-tier outlet.
            "prestige_weighted_mentions": 0.6,
            # The single strongest signal: coordinated coverage across
            # multiple outlets.
            "source_diversity": 0.8,
            # Recent mentions matter more than old ones.
            "velocity_90d": 0.4,
            # Emergence — bumped up from my first draft to catch new makers
            # faster.
            "first_seen_recency": 0.6,
        },
        independent_multiplier=True,
    ),
    "hidden_gem": Preset(
        name="hidden_gem",
        weights={
            # Dominant signal: obscurity = 1/(1+mentions). Peaks at zero mentions.
            # Under z-score normalization this cleanly separates the long tail
            # of zero-mention makers from the heavily-covered ones.
            "obscurity": 1.0,
            # If a prestige outlet did notice them, that's a positive signal
            # (the right people are starting to see it). Mild weight so it
            # refines the ordering within the obscure tail rather than
            # pulling heavily-covered makers into the rankings.
            "prestige_weighted_mentions": 0.3,
            # Emergence matters — makers newly appearing in our data are the
            # most interesting "hidden gems" among the obscure tail.
            "first_seen_recency": 0.5,
            # Everything else at zero: source_diversity and velocity are
            # inherently at odds with hidden.
        },
        independent_multiplier=False,
    ),
}


def get_preset(name: str) -> Preset:
    """Return a preset by name. Raises KeyError if unknown."""
    if name not in PRESETS:
        known = ", ".join(sorted(PRESETS.keys()))
        raise KeyError(f"Unknown preset {name!r}. Known presets: {known}")
    return PRESETS[name]
