"""Taste-affinity re-ranking — Layer 3b, the "For You" view (chunk 4I).

The discovery->engagement arc's headline and its final piece. The global presets
answer "which makers are rising / hidden right now?"; this answers "which of
them would *I* be interested in?" by re-ordering an existing ranking using the
like/dislike taste reactions captured in 3V (and gathered in bulk via the 3X
triage queue).

Three steps:

1. Affinity. For each maker, affinity = average taste similarity to the makers
   you've LIKED minus average similarity to the ones you've DISLIKED, reusing
   the same weighted-overlap engine (``taste.similarity``) that powers "makers
   like this" on the detail page. A maker is excluded from its own reference
   sets (a liked maker doesn't credit itself). Makers with no taste tags -- or
   no shared dimension with any liked/disliked maker -- are *incomparable* and
   get a neutral affinity (``None``): they keep their base position rather than
   being nudged on no evidence, matching the sparsely-tagged-isn't-penalized
   rule in ``taste.similarity``.

2. Blend. The "for you" score is the maker's existing composite under the chosen
   base preset PLUS a constant-weighted, population-z-scored affinity term -- so
   affinity enters as one more z-scored signal on the same scale the methodology
   already uses, and ``AFFINITY_WEIGHT`` reads like the preset's other signal
   weights. Incomparable makers contribute exactly 0 (no movement). The z-score
   is computed over the *comparable* makers in the ranked population, so neutral
   sits at the comparable mean -- "as if you had average feelings."

3. Re-rank. Sort by the blended score; the result is the base ranking shuffled
   by taste. It's the base preset's universe (eligibility -- rising_star's
   independent gate, hidden_gem's mention cap -- is inherited via the composite
   from ``fetch_top_makers``, not re-implemented), just re-ordered.

Everything here is derived per request from taste tags + reactions: NO schema
change, NO stored "for_you" preset, and it updates the instant you react.
Persisting it as a first-class preset stays a lossless follow-up (the
column->table logic NOTES records) if the on-the-fly cost ever bites.

Cost note: ``compute_affinities`` is O(candidates x reactions) ``similarity``
calls -- a few tens of thousands at the current population/reaction size, each
cheap (seven dict lookups). Fine for a local single-user tool, in the same
spirit as ``_similar_makers`` / ``find_near_duplicates``; revisit if it drags.
"""
from __future__ import annotations

import statistics
from typing import Any

from sqlalchemy.orm import Session

from watch_app import taste
from watch_app.extraction.maker_admin import TASTE_REACTIONS  # ("like", "dislike")
from watch_app.scoring.queries import fetch_top_makers
from watch_app.storage.models import Maker


#: Weight on the (z-scored) affinity term when blending it into the base
#: composite. It sits on the same z-scale as the preset signal weights
#: (rising_star runs 0.2-0.8), so 0.6 gives taste roughly the pull of
#: first_seen_recency / prestige -- enough to meaningfully re-order without
#: overriding a strong base signal. This is the first knob to tune once the
#: re-ranking has been looked at against real reactions; that tuning is where
#: "adaptive" tightens from here.
AFFINITY_WEIGHT = 0.6


def _zscore(values: list[float]) -> list[float]:
    """Population z-score, mirroring ranker._zscore / methodology._zscore_values.

    Duplicated (like those two) to keep this module import-light and independent
    of the ranker internals. All-equal input (zero stdev) -> all zeros, so a
    single comparable maker has z 0 and doesn't move: with no spread there's no
    population to judge its affinity against.
    """
    if not values:
        return []
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return [0.0] * len(values)
    return [(v - mean) / stdev for v in values]


def _maker_taste_dict(m: Maker) -> dict:
    """Taste field -> value for one maker (the shape ``taste.similarity`` wants)."""
    return {f: getattr(m, f) for f in taste.TASTE_FIELDS}


def _has_taste(td: dict) -> bool:
    return any(td.get(f) for f in taste.TASTE_FIELDS)


def compute_affinities(
    session: Session, maker_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Affinity for each maker in ``maker_ids``, keyed by id.

    Each value is::

        {
          "affinity": float | None,   # avg like-sim - avg dislike-sim; None when
                                      # the maker shares no populated dimension
                                      # with any liked/disliked maker (neutral)
          "nearest_like": {"maker_id", "name", "match"} | None,  # the single
                                      # liked maker this one is closest to -- the
                                      # ranking's human "why"
        }

    Reference sets are the liked / disliked makers (3V reactions), restricted to
    tagged, non-suppressed makers; a maker is excluded from its own reference
    sets. Empty reference sets (no reactions yet) yield all-neutral affinity.
    """
    # Reference makers, loaded once: split by reaction, taste dict + name kept.
    liked: dict[int, dict] = {}
    disliked: dict[int, dict] = {}
    ref_names: dict[int, str] = {}
    for m in (
        session.query(Maker)
        .filter(
            Maker.taste_reaction.in_(TASTE_REACTIONS),
            Maker.is_suppressed.is_(False),
        )
        .all()
    ):
        td = _maker_taste_dict(m)
        if not _has_taste(td):
            continue  # an untagged reaction can't inform similarity
        ref_names[m.id] = m.canonical_name
        (liked if m.taste_reaction == "like" else disliked)[m.id] = td

    candidates = (
        session.query(Maker).filter(Maker.id.in_(maker_ids)).all()
        if maker_ids
        else []
    )
    cand_taste = {m.id: _maker_taste_dict(m) for m in candidates}

    out: dict[int, dict[str, Any]] = {}
    for mid in maker_ids:
        td = cand_taste.get(mid)
        if td is None or not _has_taste(td):
            out[mid] = {"affinity": None, "nearest_like": None}
            continue

        like_sims: list[float] = []
        nearest: tuple[float, int] | None = None  # (sim, ref_id)
        for rid, rtd in liked.items():
            if rid == mid:
                continue
            s = taste.similarity(td, rtd)
            if s is None:
                continue
            like_sims.append(s)
            if nearest is None or s > nearest[0]:
                nearest = (s, rid)

        dislike_sims: list[float] = []
        for rid, rtd in disliked.items():
            if rid == mid:
                continue
            s = taste.similarity(td, rtd)
            if s is None:
                continue
            dislike_sims.append(s)

        if not like_sims and not dislike_sims:
            out[mid] = {"affinity": None, "nearest_like": None}
            continue

        like_c = statistics.mean(like_sims) if like_sims else 0.0
        dislike_c = statistics.mean(dislike_sims) if dislike_sims else 0.0
        nearest_like = (
            {
                "maker_id": nearest[1],
                "name": ref_names[nearest[1]],
                "match": round(nearest[0] * 100),
            }
            if nearest is not None
            else None
        )
        out[mid] = {"affinity": like_c - dislike_c, "nearest_like": nearest_like}
    return out


def build_for_you_ranking(
    session: Session,
    preset_name: str,
    display_limit: int = 30,
) -> dict[str, Any]:
    """Re-rank the base ``preset_name`` ranking by taste affinity.

    Returns::

        {
          "rows": [
            {
              "for_you_rank": int,        # 1-based position after re-ranking
              "base_rank": int,           # position under the base preset
              "maker_id": int, "name": str, "country": str | None,
              "is_independent": bool, "mention_count": int,
              "taste_reaction": str | None,  # your own like/dislike, if any
              "affinity": float | None,      # raw avg-like - avg-dislike
              "nearest_like": {...} | None,  # the "why"
            }, ...                           # for_you_rank ascending, <= display_limit
          ],
          "n_likes": int, "n_dislikes": int,  # reaction reference-set sizes
          "n_ranked": int,                     # makers in the base universe
          "has_reactions": bool,
        }

    The whole base universe is fetched and re-ranked (so a taste-loved maker
    ranked low globally can surface); only the top ``display_limit`` are
    returned. With no reactions, the order is exactly the base ranking.
    """
    base = fetch_top_makers(session, preset_name, limit=100_000)
    n_likes = (
        session.query(Maker)
        .filter(Maker.taste_reaction == "like", Maker.is_suppressed.is_(False))
        .count()
    )
    n_dislikes = (
        session.query(Maker)
        .filter(Maker.taste_reaction == "dislike", Maker.is_suppressed.is_(False))
        .count()
    )
    if not base:
        return {
            "rows": [],
            "n_likes": n_likes,
            "n_dislikes": n_dislikes,
            "n_ranked": 0,
            "has_reactions": bool(n_likes or n_dislikes),
        }

    maker_ids = [r["maker_id"] for r in base]
    affinities = compute_affinities(session, maker_ids)

    # Z-score the comparable affinities across the ranked population; neutral
    # (None) makers get 0 -- exactly no movement from their base composite.
    comparable_ids = [
        mid for mid in maker_ids if affinities[mid]["affinity"] is not None
    ]
    z_by_id: dict[int, float] = {mid: 0.0 for mid in maker_ids}
    if comparable_ids:
        zs = _zscore([affinities[mid]["affinity"] for mid in comparable_ids])
        for mid, z in zip(comparable_ids, zs):
            z_by_id[mid] = z

    # Blend, then re-rank. Tie-break by base rank, then id, for determinism.
    base_by_id = {r["maker_id"]: r for r in base}
    blended = [
        (r["score"] + AFFINITY_WEIGHT * z_by_id[r["maker_id"]], r["rank"], r["maker_id"])
        for r in base
    ]
    blended.sort(key=lambda t: (-t[0], t[1], t[2]))

    display = blended[:display_limit]
    display_ids = [mid for (_s, _b, mid) in display]
    # Badge fields fetch_top_makers doesn't carry, only for the rows shown.
    badge = {
        m.id: (m.is_independent, m.taste_reaction)
        for m in session.query(Maker).filter(Maker.id.in_(display_ids)).all()
    }

    rows: list[dict[str, Any]] = []
    for pos, (_score, _brank, mid) in enumerate(display, start=1):
        r = base_by_id[mid]
        aff = affinities[mid]
        is_indep, reaction = badge.get(mid, (True, None))
        rows.append(
            {
                "for_you_rank": pos,
                "base_rank": r["rank"],
                "maker_id": mid,
                "name": r["canonical_name"],
                "country": r["country"],
                "is_independent": is_indep,
                "mention_count": r["mention_count"],
                "taste_reaction": reaction,
                "affinity": aff["affinity"],
                "nearest_like": aff["nearest_like"],
            }
        )

    return {
        "rows": rows,
        "n_likes": n_likes,
        "n_dislikes": n_dislikes,
        "n_ranked": len(base),
        "has_reactions": bool(n_likes or n_dislikes),
    }
