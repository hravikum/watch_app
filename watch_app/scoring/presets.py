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

    #: If set, makers with mention_count strictly greater than this value get
    #: a zero score under this preset. Used by hidden_gem to enforce the
    #: semantic that "hidden" requires actually being obscure. None = no cap.
    max_mentions_for_eligibility: int | None = None


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
            # Dominant signal — and under z-score normalization, an overwhelmingly
            # dominant one. We want zero-mention makers to win decisively over
            # 1-mention makers. An earlier version of this preset used 0.3/0.5
            # weights on the other two signals, which is enough to flip the
            # ordering when 1-mention GPHG nominees get high recency/prestige
            # values. Crushing those two weights keeps them as pure tie-breakers
            # within the large zero-mention population.
            "obscurity": 1.0,
            # Tiny positive weight — if two makers are tied on obscurity (both
            # zero mentions), whoever's had even a quiet prestige nod edges
            # ahead.
            "prestige_weighted_mentions": 0.05,
            # Also a tiny weight — emergence refines ordering within the obscure
            # tail, doesn't pull well-covered makers into the rankings.
            "first_seen_recency": 0.05,
        },
        independent_multiplier=False,
        # A maker with 3+ mentions isn't "hidden" by any reasonable definition.
        # This is a hard gate; even with favorable signal mix, they're out.
        max_mentions_for_eligibility=2,
    ),
}


def get_preset(name: str) -> Preset:
    """Return a preset by name. Raises KeyError if unknown."""
    if name not in PRESETS:
        known = ", ".join(sorted(PRESETS.keys()))
        raise KeyError(f"Unknown preset {name!r}. Known presets: {known}")
    return PRESETS[name]
