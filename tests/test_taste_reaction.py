"""Tests for personal taste reaction (Layer 3a — reaction capture).

Covers:
  1. set_taste_reaction helper — valid value, clear, invalid → ValueError,
     timestamp stamped on set, cleared on clear, and NOT restamped on a no-op
     re-save (the "when did I first react" the data clock needs).
  2. POST /makers/{id}/state — also sets the reaction, redirects; invalid → 400;
     missing maker → 404; orthogonal to relationship_state (set in one POST).
  3. Detail dict carries taste_reaction + taste_reaction_at.

The reaction is ORTHOGONAL to suppression and to relationship_state: it never
changes visibility, and it's a taste signal, not an engagement posture.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import (
    _fetch_maker_detail,
    router as makers_router,
)
from watch_app.extraction.maker_admin import set_taste_reaction
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app, follow_redirects=False)


# ---------- helper: set_taste_reaction ----------


def test_set_reaction_stamps_timestamp(session):
    m = Maker(canonical_name="Like Co", is_independent=True)
    session.add(m)
    session.flush()
    set_taste_reaction(session, m, "like")
    assert m.taste_reaction == "like"
    assert m.taste_reaction_at is not None


def test_clear_reaction_clears_timestamp(session):
    m = Maker(canonical_name="Clear Co", is_independent=True, taste_reaction="dislike")
    session.add(m)
    session.flush()
    set_taste_reaction(session, m, None)
    assert m.taste_reaction is None
    assert m.taste_reaction_at is None


def test_invalid_reaction_raises(session):
    m = Maker(canonical_name="Bad Co", is_independent=True)
    session.add(m)
    session.flush()
    with pytest.raises(ValueError):
        set_taste_reaction(session, m, "love")


def test_resaving_same_reaction_preserves_timestamp(session):
    """A no-op re-save must NOT bump taste_reaction_at — the original react
    time is the signal the data clock and Layer 3b windowing rely on."""
    m = Maker(canonical_name="Stable Co", is_independent=True)
    session.add(m)
    session.flush()
    set_taste_reaction(session, m, "like")
    first_time = m.taste_reaction_at
    assert first_time is not None
    set_taste_reaction(session, m, "like")  # same value again
    assert m.taste_reaction_at == first_time  # unchanged


def test_changing_reaction_restamps_timestamp(session):
    m = Maker(canonical_name="Flip Co", is_independent=True)
    session.add(m)
    session.flush()
    set_taste_reaction(session, m, "like")
    first_time = m.taste_reaction_at
    set_taste_reaction(session, m, "dislike")
    assert m.taste_reaction == "dislike"
    assert m.taste_reaction_at is not None
    assert m.taste_reaction_at >= first_time


# ---------- route: POST /makers/{id}/state also sets reaction ----------


def test_post_sets_reaction_and_redirects(client):
    with session_scope() as s:
        s.add(Maker(canonical_name="Route Co", is_independent=True))
    with session_scope() as s:
        mid = s.query(Maker).filter_by(canonical_name="Route Co").one().id

    r = client.post(f"/makers/{mid}/state", data={"reaction": "like"})
    assert r.status_code == 303
    with session_scope() as s:
        m = s.get(Maker, mid)
        assert m.taste_reaction == "like"
        assert m.taste_reaction_at is not None


def test_post_sets_state_and_reaction_together(client):
    """One 'Your status' form carries both; they're stored independently."""
    with session_scope() as s:
        s.add(Maker(canonical_name="Both Co", is_independent=True))
    with session_scope() as s:
        mid = s.query(Maker).filter_by(canonical_name="Both Co").one().id

    r = client.post(
        f"/makers/{mid}/state",
        data={"state": "watching", "reaction": "dislike", "notes": "n"},
    )
    assert r.status_code == 303
    with session_scope() as s:
        m = s.get(Maker, mid)
        assert m.relationship_state == "watching"
        assert m.taste_reaction == "dislike"
        assert m.notes == "n"


def test_post_invalid_reaction_is_400(client):
    with session_scope() as s:
        s.add(Maker(canonical_name="Bad Route", is_independent=True))
    with session_scope() as s:
        mid = s.query(Maker).filter_by(canonical_name="Bad Route").one().id

    r = client.post(f"/makers/{mid}/state", data={"reaction": "love"})
    assert r.status_code == 400


def test_post_clear_reaction(client):
    with session_scope() as s:
        s.add(Maker(canonical_name="Clr Route", is_independent=True,
                    taste_reaction="like"))
    with session_scope() as s:
        mid = s.query(Maker).filter_by(canonical_name="Clr Route").one().id

    r = client.post(f"/makers/{mid}/state", data={"reaction": ""})
    assert r.status_code == 303
    with session_scope() as s:
        m = s.get(Maker, mid)
        assert m.taste_reaction is None
        assert m.taste_reaction_at is None


def test_post_missing_maker_is_404(client):
    r = client.post("/makers/999999/state", data={"reaction": "like"})
    assert r.status_code == 404


# ---------- detail dict ----------


def test_detail_carries_reaction(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name="Detail Co", is_independent=True,
                    taste_reaction="like"))
    with session_scope() as s:
        mid = s.query(Maker).filter_by(canonical_name="Detail Co").one().id
    detail = _fetch_maker_detail(mid)
    assert detail["taste_reaction"] == "like"
    assert "taste_reaction_at" in detail
