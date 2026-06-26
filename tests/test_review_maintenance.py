"""Tests for the maker-maintenance controls surfaced on the review grid.

Suppress / delete were put on the maker detail page in 4D; this chunk also
surfaces them on /review so a discovered maker can be curated without opening
its page. The underlying operations (suppress_maker / delete_maker, dry-run +
seed guard) are covered by test_suppression.py, and the detail-page wiring by
test_maker_admin_ui.py. These assert the *review-grid* routes: the admin gate,
that the controls render, suppress (which drops the maker off the grid), the
in-card delete confirm panel (dry-run counts), the delete commit + redirect
back to /review, and that a seed maker is blocked (409) rather than deleted.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.config import get_settings
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Source as SourceRow


SEED_NAME = "Simon Brette"  # present in the seed YAML (same anchor test_review uses)


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


@pytest.fixture
def admin_on(monkeypatch):
    monkeypatch.setenv("WATCH_APP_ADMIN", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _add_maker(name: str = "Zzz Review Maint Maker", **kwargs) -> int:
    with session_scope() as session:
        m = Maker(canonical_name=name, is_independent=True, **kwargs)
        session.add(m)
        session.flush()
        return m.id


def _add_maker_with_mention() -> int:
    with session_scope() as session:
        src = SourceRow(name="ReviewMaintSrc", category="media", prestige_weight=0.5)
        m = Maker(canonical_name="Zzz Review Delete Me", is_independent=True)
        session.add_all([src, m])
        session.flush()
        session.add(
            Mention(maker_id=m.id, source_id=src.id, url="http://x/1", title="t")
        )
        return m.id


# ---------- gating ----------


def test_review_suppress_404_when_admin_off(client):
    mid = _add_maker()
    r = client.post(f"/review/suppress/{mid}", follow_redirects=False)
    assert r.status_code == 404


def test_review_delete_404_when_admin_off(client):
    mid = _add_maker()
    r = client.post(f"/review/delete/{mid}", follow_redirects=False)
    assert r.status_code == 404


# ---------- controls render (admin on) ----------


def test_review_grid_shows_maintenance_controls(client, admin_on):
    mid = _add_maker()
    r = client.get("/review")
    assert r.status_code == 200
    assert f"/review/suppress/{mid}" in r.text
    assert f"/review?confirm=delete&maker={mid}" in r.text


# ---------- suppress ----------


def test_review_suppress_sets_flag_and_redirects(client, admin_on):
    mid = _add_maker()
    r = client.post(f"/review/suppress/{mid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/review"
    with session_scope() as session:
        assert session.get(Maker, mid).is_suppressed is True


def test_review_suppressed_maker_drops_off_grid(client, admin_on):
    name = "Zzz Review Drops Off"
    mid = _add_maker(name=name)
    assert name in client.get("/review").text
    client.post(f"/review/suppress/{mid}")
    assert name not in client.get("/review").text


# ---------- delete: confirm panel + commit ----------


def test_review_delete_confirm_panel_shows_counts(client, admin_on):
    mid = _add_maker_with_mention()
    r = client.get(f"/review?confirm=delete&maker={mid}")
    assert r.status_code == 200
    assert "Permanently delete" in r.text
    # Dry-run impact is shown (one mention, nothing else).
    assert "1 mention" in r.text
    # The confirm form posts to the review-grid delete route.
    assert f'action="/review/delete/{mid}"' in r.text


def test_review_delete_no_panel_without_confirm(client, admin_on):
    mid = _add_maker_with_mention()
    r = client.get("/review")
    assert r.status_code == 200
    assert "Permanently delete" not in r.text


def test_review_delete_commits_and_redirects_to_review(client, admin_on):
    mid = _add_maker_with_mention()
    r = client.post(f"/review/delete/{mid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/review"
    with session_scope() as session:
        assert session.get(Maker, mid) is None


# ---------- seed guard ----------


def test_review_delete_blocked_for_seed_maker(client, admin_on):
    """A seed maker can't be deleted — the loader would re-create it. The POST is
    a 409 rather than a deletion. (Seed makers aren't shown on /review, so this
    guard is a backstop for a hand-crafted request.)"""
    mid = _add_maker(name=SEED_NAME)
    r = client.post(f"/review/delete/{mid}", follow_redirects=False)
    assert r.status_code == 409
    with session_scope() as session:
        assert session.get(Maker, mid) is not None
