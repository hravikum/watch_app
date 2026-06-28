"""Tests for the For You taste-personalized ranking (Layer 3b, chunk 4I).

Covers:
  1. compute_affinities      — like/dislike averaging, self-exclusion, neutral
  2. build_for_you_ranking   — re-rank, z-score behavior, caps, counts
  3. the /for-you route       — render, preset switch, fallback, onboarding, nav
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.scoring.affinity import (
    build_for_you_ranking,
    compute_affinities,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Score


SOLO_CLASSICAL = dict(maker_type="solo-watchmaker", aesthetic_tags=["classical"])
MICRO_TOOL = dict(maker_type="microbrand", aesthetic_tags=["tool"])


def _add(
    name,
    *,
    reaction=None,
    score=None,
    rank=None,
    preset="rising_star",
    suppressed=False,
    independent=True,
    **taste_kw,
) -> int:
    """Create a maker (+ optional taste, reaction, and one Score row); return id."""
    with session_scope() as session:
        m = Maker(
            canonical_name=name,
            is_independent=independent,
            is_suppressed=suppressed,
            taste_reaction=reaction,
            **taste_kw,
        )
        session.add(m)
        session.flush()
        if score is not None:
            session.add(
                Score(
                    maker_id=m.id,
                    preset=preset,
                    value=score,
                    rank=rank,
                    computed_at=datetime.now(timezone.utc),
                )
            )
        return m.id


def _affinity(maker_ids):
    with session_scope() as session:
        return compute_affinities(session, maker_ids)


def _ranking(preset="rising_star", display_limit=30):
    with session_scope() as session:
        return build_for_you_ranking(session, preset, display_limit=display_limit)


# ---------- compute_affinities ----------


def test_liked_neighbor_is_positive(test_engine):
    _add("Liked", reaction="like", **SOLO_CLASSICAL)
    cand = _add("Cand", **SOLO_CLASSICAL)  # identical taste to the liked maker
    a = _affinity([cand])[cand]
    assert a["affinity"] == 1.0
    assert a["nearest_like"]["name"] == "Liked"
    assert a["nearest_like"]["match"] == 100


def test_disliked_neighbor_is_negative(test_engine):
    _add("Disliked", reaction="dislike", **MICRO_TOOL)
    cand = _add("Cand", **MICRO_TOOL)
    a = _affinity([cand])[cand]
    assert a["affinity"] == -1.0
    assert a["nearest_like"] is None  # no likes to point at


def test_like_and_dislike_net_out(test_engine):
    _add("Liked", reaction="like", **SOLO_CLASSICAL)
    _add("Disliked", reaction="dislike", **SOLO_CLASSICAL)  # candidate matches both
    cand = _add("Cand", **SOLO_CLASSICAL)
    a = _affinity([cand])[cand]
    assert a["affinity"] == 0.0  # 1.0 like − 1.0 dislike


def test_untagged_candidate_is_neutral(test_engine):
    _add("Liked", reaction="like", **SOLO_CLASSICAL)
    cand = _add("Bare")  # no taste at all
    a = _affinity([cand])[cand]
    assert a["affinity"] is None
    assert a["nearest_like"] is None


def test_candidate_excluded_from_own_reference(test_engine):
    # The only liked maker is also the candidate: with self excluded there is no
    # reference left, so it is neutral rather than scoring 1.0 against itself.
    liked = _add("Liked", reaction="like", **SOLO_CLASSICAL)
    a = _affinity([liked])[liked]
    assert a["affinity"] is None


def test_no_reactions_all_neutral(test_engine):
    c1 = _add("A", **SOLO_CLASSICAL)
    c2 = _add("B", **MICRO_TOOL)
    out = _affinity([c1, c2])
    assert out[c1]["affinity"] is None
    assert out[c2]["affinity"] is None


def test_incomparable_tagged_candidate_is_neutral(test_engine):
    # Candidate is tagged, but only on a dimension the (tagged) liked maker never
    # populates → no shared dimension → incomparable → neutral.
    _add("Liked", reaction="like", maker_type="solo-watchmaker")
    cand = _add("Cand", price_band="haute")
    assert _affinity([cand])[cand]["affinity"] is None


# ---------- build_for_you_ranking ----------


def test_liked_maker_climbs_over_disliked(test_engine):
    _add("L", reaction="like", **SOLO_CLASSICAL)
    _add("D", reaction="dislike", **MICRO_TOOL)
    # Two scored candidates: the disliked-aligned one is #1 at base, the
    # liked-aligned one #2 — close scores so affinity flips them.
    near_dislike = _add("NearDislike", score=1.0, rank=1, **MICRO_TOOL)
    near_like = _add("NearLike", score=0.9, rank=2, **SOLO_CLASSICAL)

    out = _ranking()
    order = [r["maker_id"] for r in out["rows"]]
    assert order[0] == near_like  # taste lifted it past the base #1
    assert order[1] == near_dislike
    top = out["rows"][0]
    assert top["base_rank"] == 2 and top["for_you_rank"] == 1
    assert top["affinity"] == 1.0
    assert top["nearest_like"]["name"] == "L"


def test_neutral_makers_keep_base_order(test_engine):
    _add("L", reaction="like", **SOLO_CLASSICAL)
    # Both candidates untagged → neutral → order unchanged from base.
    x = _add("X", score=2.0, rank=1)
    y = _add("Y", score=1.0, rank=2)
    order = [r["maker_id"] for r in _ranking()["rows"]]
    assert order == [x, y]


def test_single_comparable_does_not_move(test_engine):
    # One comparable maker among neutrals: z-score of a single value is 0, so no
    # spread → no movement, even with a strong raw affinity. (Documented.)
    _add("L", reaction="like", **SOLO_CLASSICAL)
    top = _add("Neutral", score=2.0, rank=1)
    liked_cand = _add("LikedCand", score=1.0, rank=2, **SOLO_CLASSICAL)
    rows = _ranking()["rows"]
    assert [r["maker_id"] for r in rows] == [top, liked_cand]
    # raw affinity is still surfaced even though it didn't move the maker
    assert rows[1]["affinity"] == 1.0


def test_no_reactions_returns_base_order(test_engine):
    a = _add("A", score=1.0, rank=2, **SOLO_CLASSICAL)
    b = _add("B", score=2.0, rank=1, **MICRO_TOOL)
    out = _ranking()
    assert out["has_reactions"] is False
    assert [r["maker_id"] for r in out["rows"]] == [b, a]  # pure base order


def test_empty_population(test_engine):
    _add("L", reaction="like", **SOLO_CLASSICAL)  # a reaction, but nothing scored
    out = _ranking()
    assert out["rows"] == []
    assert out["n_ranked"] == 0
    assert out["has_reactions"] is True


def test_display_limit_caps_rows(test_engine):
    _add("L", reaction="like", **SOLO_CLASSICAL)
    for i in range(5):
        _add(f"M{i}", score=float(5 - i), rank=i + 1, **SOLO_CLASSICAL)
    out = _ranking(display_limit=3)
    assert len(out["rows"]) == 3
    assert out["n_ranked"] == 5  # whole universe ranked, only top-3 returned


def test_reference_counts_exclude_suppressed(test_engine):
    _add("L1", reaction="like", **SOLO_CLASSICAL)
    _add("L2", reaction="like", **SOLO_CLASSICAL)
    _add("D1", reaction="dislike", **MICRO_TOOL)
    _add("Lsupp", reaction="like", suppressed=True, **SOLO_CLASSICAL)
    _add("Scored", score=1.0, rank=1, **SOLO_CLASSICAL)
    out = _ranking()
    assert out["n_likes"] == 2  # the suppressed like is not counted
    assert out["n_dislikes"] == 1


# ---------- the /for-you route ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def _seed_route_population():
    _add("L", reaction="like", **SOLO_CLASSICAL)
    _add("D", reaction="dislike", **MICRO_TOOL)
    _add("NearDislike", score=1.0, rank=1, **MICRO_TOOL)
    _add("NearLike", score=0.9, rank=2, **SOLO_CLASSICAL)
    # a hidden_gem-scored row so the preset switch has data too
    _add("GemA", score=1.0, rank=1, preset="hidden_gem", **SOLO_CLASSICAL)


def test_route_renders_reranked(client):
    _seed_route_population()
    r = client.get("/for-you?preset=rising_star")
    assert r.status_code == 200
    assert "For You" in r.text
    assert "NearLike" in r.text
    assert "Closest like" in r.text  # the "why" column header


def test_route_preset_switch(client):
    _seed_route_population()
    r = client.get("/for-you?preset=hidden_gem")
    assert r.status_code == 200
    assert "GemA" in r.text


def test_route_unknown_preset_falls_back(client):
    _seed_route_population()
    r = client.get("/for-you?preset=bogus")
    assert r.status_code == 200  # _resolve_preset → default, no crash
    assert "NearLike" in r.text  # default is rising_star


def test_route_onboarding_without_reactions(client):
    _add("Scored", score=1.0, rank=1, **SOLO_CLASSICAL)  # scored, but no reactions
    r = client.get("/for-you")
    assert r.status_code == 200
    assert "haven't reacted" in r.text
    assert "/triage" in r.text


def test_nav_has_for_you_link(client):
    r = client.get("/for-you")
    assert 'href="/for-you"' in r.text
