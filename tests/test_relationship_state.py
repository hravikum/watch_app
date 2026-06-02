"""Tests for personal relationship state + notes (chunk B).

Covers:
  1. set_relationship_state helper — valid state, clear, invalid → ValueError,
     notes strip/clear
  2. POST /makers/{id}/state — sets state + notes, redirects; invalid → 400;
     missing maker → 404
  3. Detail dict carries relationship_state + notes
  4. Dashboard ranking filter by state (_fetch_top_for_template)
  5. Explore universe narrowing by state (_explore)

State is ORTHOGONAL to suppression: it never changes visibility on its own.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import (
    _explore,
    _fetch_maker_detail,
    _fetch_top_for_template,
    router as makers_router,
)
from watch_app.extraction.maker_admin import set_relationship_state
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Score


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    # Don't follow the POST-redirect so we can assert the 303 itself.
    return TestClient(app, follow_redirects=False)


# ---------- helper: set_relationship_state ----------


def test_set_state_and_notes(session):
    m = Maker(canonical_name="State Co", is_independent=True)
    session.add(m)
    session.flush()
    set_relationship_state(session, m, "watching", "  contact via gallery  ")
    assert m.relationship_state == "watching"
    assert m.notes == "contact via gallery"  # stripped


def test_clear_state_and_notes(session):
    m = Maker(canonical_name="Clear Co", is_independent=True,
              relationship_state="target", notes="x")
    session.add(m)
    session.flush()
    set_relationship_state(session, m, None, "   ")
    assert m.relationship_state is None
    assert m.notes is None  # whitespace-only clears


def test_invalid_state_raises(session):
    m = Maker(canonical_name="Bad Co", is_independent=True)
    session.add(m)
    session.flush()
    with pytest.raises(ValueError):
        set_relationship_state(session, m, "nonsense", "")


# ---------- route: POST /makers/{id}/state ----------


def test_post_state_sets_and_redirects(client):
    with session_scope() as s:
        s.add(Maker(canonical_name="Route Co", is_independent=True))
    with session_scope() as s:
        mid = s.query(Maker).filter_by(canonical_name="Route Co").one().id

    r = client.post(f"/makers/{mid}/state", data={"state": "target", "notes": "lead"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/makers/{mid}"

    with session_scope() as s:
        m = s.query(Maker).filter_by(id=mid).one()
        assert m.relationship_state == "target"
        assert m.notes == "lead"


def test_post_state_invalid_is_400(client):
    with session_scope() as s:
        s.add(Maker(canonical_name="Inv Co", is_independent=True))
    with session_scope() as s:
        mid = s.query(Maker).filter_by(canonical_name="Inv Co").one().id
    r = client.post(f"/makers/{mid}/state", data={"state": "bogus"})
    assert r.status_code == 400


def test_post_state_missing_maker_is_404(client):
    r = client.post("/makers/999999/state", data={"state": "target"})
    assert r.status_code == 404


# ---------- detail dict ----------


def test_detail_carries_state_and_notes(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name="Detail Co", is_independent=True,
                    relationship_state="known", notes="seen at Dubai"))
    with session_scope() as s:
        mid = s.query(Maker).filter_by(canonical_name="Detail Co").one().id
    detail = _fetch_maker_detail(mid)
    assert detail["relationship_state"] == "known"
    assert detail["notes"] == "seen at Dubai"


# ---------- dashboard ranking filter ----------


def test_dashboard_filter_by_state(test_engine):
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        watched = Maker(canonical_name="Watched", is_independent=True,
                        relationship_state="watching")
        other = Maker(canonical_name="Other", is_independent=True)
        s.add_all([watched, other])
        s.flush()
        s.add_all([
            Score(maker_id=watched.id, preset="rising_star", value=5.0, rank=1, computed_at=now),
            Score(maker_id=other.id, preset="rising_star", value=4.0, rank=2, computed_at=now),
        ])

    unfiltered = [r["name"] for r in _fetch_top_for_template("rising_star")]
    assert "Watched" in unfiltered and "Other" in unfiltered

    filtered = [r["name"] for r in _fetch_top_for_template("rising_star", state="watching")]
    assert filtered == ["Watched"]


# ---------- explore universe narrowing ----------


def test_explore_filter_by_state(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name="Tagged Target", is_independent=True,
                    aesthetic_tags=["tool"], relationship_state="target"))
        s.add(Maker(canonical_name="Tagged None", is_independent=True,
                    aesthetic_tags=["tool"]))

    all_names = [m["name"] for m in _explore("rising_star", filters={})["makers"]]
    assert "Tagged Target" in all_names and "Tagged None" in all_names

    data = _explore("rising_star", filters={}, state="target")
    names = [m["name"] for m in data["makers"]]
    assert names == ["Tagged Target"]
    assert data["total_tagged"] == 1  # facet universe also narrowed
