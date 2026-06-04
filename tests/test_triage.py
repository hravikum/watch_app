"""Tests for the triage queue (Layer 3a workflow, chunk 3X).

Triage is the workflow half of reaction capture: a queue of every untriaged
maker (taste_reaction unset, suppressed excluded) that you react to in one
pass. Reacting drops a maker from the queue. Covers:
  1. _fetch_triage_queue — includes untriaged, excludes reacted + suppressed,
     orders scored-before-unscored.
  2. GET /triage — renders the queue (and the empty state).
  3. POST /triage/{id} — sets the reaction and returns an HTMX fragment (NOT a
     303 redirect like the detail-page form); invalid → 400, missing → 404;
     reacting removes the maker from the next queue.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import _fetch_triage_queue, router as makers_router
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Score


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app, follow_redirects=False)


def _add_maker(**kwargs) -> int:
    defaults = {"is_independent": True}
    defaults.update(kwargs)
    with session_scope() as s:
        m = Maker(**defaults)
        s.add(m)
        s.flush()
        return m.id


# ---------- _fetch_triage_queue ----------


def test_queue_includes_untriaged(test_engine):
    mid = _add_maker(canonical_name="Fresh Co")
    assert mid in [r["maker_id"] for r in _fetch_triage_queue()]


def test_queue_excludes_reacted(test_engine):
    _add_maker(canonical_name="Reacted Co", taste_reaction="like")
    assert "Reacted Co" not in [r["name"] for r in _fetch_triage_queue()]


def test_queue_excludes_suppressed(test_engine):
    """A suppressed maker stays out of triage even when it has no reaction —
    you hid it deliberately; resurfacing it would re-introduce the noise."""
    _add_maker(canonical_name="Hidden Co", is_suppressed=True)
    assert "Hidden Co" not in [r["name"] for r in _fetch_triage_queue()]


def test_queue_orders_scored_before_unscored(test_engine):
    scored_id = _add_maker(canonical_name="Scored Co")
    _add_maker(canonical_name="Unscored Co")
    with session_scope() as s:
        s.add(
            Score(
                maker_id=scored_id,
                preset="rising_star",
                value=5.0,
                rank=1,
                computed_at=datetime.now(timezone.utc),
            )
        )

    queue = _fetch_triage_queue()
    names = [r["name"] for r in queue]
    assert names.index("Scored Co") < names.index("Unscored Co")

    scored_row = next(r for r in queue if r["name"] == "Scored Co")
    unscored_row = next(r for r in queue if r["name"] == "Unscored Co")
    assert scored_row["rank"] == 1
    assert unscored_row["rank"] is None


# ---------- GET /triage ----------


def test_get_triage_renders(client):
    _add_maker(canonical_name="Visible Co")
    r = client.get("/triage")
    assert r.status_code == 200
    assert "Visible Co" in r.text


def test_get_triage_empty_state(client):
    _add_maker(canonical_name="Done Co", taste_reaction="dislike")
    r = client.get("/triage")
    assert r.status_code == 200
    assert "reacted to every maker" in r.text


# ---------- POST /triage/{id} ----------


def test_post_react_sets_and_returns_fragment(client):
    mid = _add_maker(canonical_name="React Co")
    r = client.post(f"/triage/{mid}", data={"reaction": "like"})
    assert r.status_code == 200  # fragment, NOT a 303 redirect
    with session_scope() as s:
        m = s.get(Maker, mid)
        assert m.taste_reaction == "like"
        assert m.taste_reaction_at is not None
    assert 'id="triage-count"' in r.text  # OOB count update present


def test_post_react_removes_from_queue(client):
    mid = _add_maker(canonical_name="Gone Co")
    assert "Gone Co" in [r["name"] for r in _fetch_triage_queue()]
    client.post(f"/triage/{mid}", data={"reaction": "dislike"})
    assert "Gone Co" not in [r["name"] for r in _fetch_triage_queue()]


def test_post_remaining_count_decrements(client):
    a = _add_maker(canonical_name="A Co")
    _add_maker(canonical_name="B Co")
    client.post(f"/triage/{a}", data={"reaction": "like"})
    assert len(_fetch_triage_queue()) == 1


def test_post_invalid_reaction_is_400(client):
    mid = _add_maker(canonical_name="Bad Co")
    assert client.post(f"/triage/{mid}", data={"reaction": "love"}).status_code == 400


def test_post_empty_reaction_is_400(client):
    """The queue offers no 'clear' — an empty reaction is rejected here, unlike
    the detail-page form where '' clears the reaction."""
    mid = _add_maker(canonical_name="Empty Co")
    assert client.post(f"/triage/{mid}", data={"reaction": ""}).status_code == 400


def test_post_missing_maker_is_404(client):
    assert client.post("/triage/999999", data={"reaction": "like"}).status_code == 404
