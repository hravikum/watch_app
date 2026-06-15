"""Tests for the maker-maintenance UI (chunk 4D): suppress / delete surfaced on
the detail page, admin-gated.

The underlying operations (suppress_maker / delete_maker, dry-run + seed guard)
are covered by test_suppression.py. These assert the *routes* wire them up: the
admin gate, that the controls render only in admin mode, the suppress/unsuppress
toggle, the delete confirm panel (dry-run counts), the commit + redirect, and
that a seed maker is blocked rather than deleted.
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


def _add_maker(name: str = "Zzz UI Test Maker", **kwargs) -> int:
    with session_scope() as session:
        m = Maker(canonical_name=name, is_independent=True, **kwargs)
        session.add(m)
        session.flush()
        return m.id


def _add_maker_with_mention() -> int:
    with session_scope() as session:
        src = SourceRow(name="UISrc", category="media", prestige_weight=0.5)
        m = Maker(canonical_name="Zzz UI Delete Me", is_independent=True)
        session.add_all([src, m])
        session.flush()
        session.add(
            Mention(maker_id=m.id, source_id=src.id, url="http://x/1", title="t")
        )
        return m.id


# ---------- gating ----------


def test_suppress_post_404_when_admin_off(client):
    mid = _add_maker()
    r = client.post(f"/makers/{mid}/suppress", follow_redirects=False)
    assert r.status_code == 404


def test_unsuppress_post_404_when_admin_off(client):
    mid = _add_maker(is_suppressed=True)
    r = client.post(f"/makers/{mid}/unsuppress", follow_redirects=False)
    assert r.status_code == 404


def test_delete_post_404_when_admin_off(client):
    mid = _add_maker()
    r = client.post(f"/makers/{mid}/delete", follow_redirects=False)
    assert r.status_code == 404


def test_detail_hides_manage_controls_when_admin_off(client):
    mid = _add_maker()
    r = client.get(f"/makers/{mid}")
    assert r.status_code == 200
    assert f"/makers/{mid}/suppress" not in r.text
    assert "?confirm=delete" not in r.text


def test_confirm_delete_query_ignored_when_admin_off(client):
    """No delete panel leaks via the query param when admin is off."""
    mid = _add_maker_with_mention()
    r = client.get(f"/makers/{mid}?confirm=delete")
    assert r.status_code == 200
    assert "Confirm delete" not in r.text


# ---------- controls render (admin on) ----------


def test_detail_shows_manage_controls_when_admin_on(client, admin_on):
    mid = _add_maker()
    r = client.get(f"/makers/{mid}")
    assert r.status_code == 200
    assert f"/makers/{mid}/suppress" in r.text
    assert f"/makers/{mid}?confirm=delete" in r.text


# ---------- suppress / unsuppress ----------


def test_suppress_sets_flag_and_redirects(client, admin_on):
    mid = _add_maker()
    r = client.post(
        f"/makers/{mid}/suppress",
        data={"reason": "out of scope"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/makers/{mid}"
    with session_scope() as session:
        m = session.get(Maker, mid)
        assert m.is_suppressed is True
        assert m.suppressed_reason == "out of scope"


def test_suppressed_maker_shows_badge_and_unsuppress(client, admin_on):
    mid = _add_maker()
    client.post(f"/makers/{mid}/suppress", data={"reason": ""})
    r = client.get(f"/makers/{mid}")
    assert "Suppressed" in r.text
    assert f"/makers/{mid}/unsuppress" in r.text


def test_unsuppress_clears_flag_and_redirects(client, admin_on):
    mid = _add_maker(is_suppressed=True, suppressed_reason="x")
    r = client.post(f"/makers/{mid}/unsuppress", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/makers/{mid}"
    with session_scope() as session:
        m = session.get(Maker, mid)
        assert m.is_suppressed is False
        assert m.suppressed_reason is None


# ---------- delete: confirm panel + commit ----------


def test_delete_confirm_panel_shows_counts(client, admin_on):
    mid = _add_maker_with_mention()
    r = client.get(f"/makers/{mid}?confirm=delete")
    assert r.status_code == 200
    assert "Confirm delete" in r.text
    # The confirm form posts to the delete route.
    assert f'action="/makers/{mid}/delete"' in r.text
    # Dry-run impact is shown (one mention, nothing else).
    assert "1 mention" in r.text


def test_delete_commits_and_redirects_to_discoveries(client, admin_on):
    mid = _add_maker_with_mention()
    r = client.post(f"/makers/{mid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/discoveries"
    with session_scope() as session:
        assert session.get(Maker, mid) is None


def test_delete_blocked_for_seed_maker(client, admin_on):
    """A seed maker: the confirm panel shows the blocker (no confirm button), and
    the POST is a 409 rather than a deletion."""
    mid = _add_maker(name=SEED_NAME)
    r = client.get(f"/makers/{mid}?confirm=delete")
    assert r.status_code == 200
    assert "Can't delete" in r.text
    assert "Confirm delete" not in r.text

    r2 = client.post(f"/makers/{mid}/delete", follow_redirects=False)
    assert r2.status_code == 409
    with session_scope() as session:
        assert session.get(Maker, mid) is not None


# =====================================================================
# Merge UI (chunk 4E): near-duplicate suggestions + typeahead picker,
# confirm panel, commit + redirect to survivor, blockers.
# =====================================================================


def _add_two_with_mention() -> tuple[int, int]:
    """A source maker (1 mention) and a target maker; returns (source, target)."""
    with session_scope() as session:
        src = SourceRow(name="MSrc", category="media", prestige_weight=0.5)
        a = Maker(canonical_name="Zzz Merge Source", is_independent=True)
        b = Maker(canonical_name="Zzz Merge Target", is_independent=True)
        session.add_all([src, a, b])
        session.flush()
        session.add(
            Mention(maker_id=a.id, source_id=src.id, url="http://m/1", title="t")
        )
        return a.id, b.id


def _add_two_similar_names() -> tuple[int, int]:
    """Two makers whose names are a near-duplicate (WRatio in [85, 100))."""
    a = _add_maker(name="Zzz Dup Brand")
    b = _add_maker(name="Zzz Dupe Brand")
    return a, b


# ---------- gating ----------


def test_merge_search_404_when_admin_off(client):
    mid = _add_maker()
    r = client.get(f"/makers/{mid}/merge/search?q=test")
    assert r.status_code == 404


def test_merge_post_404_when_admin_off(client):
    a = _add_maker(name="A One")
    b = _add_maker(name="B Two")
    r = client.post(f"/makers/{a}/merge", data={"into": str(b)}, follow_redirects=False)
    assert r.status_code == 404


def test_detail_hides_merge_controls_when_admin_off(client):
    mid = _add_maker()
    r = client.get(f"/makers/{mid}")
    assert r.status_code == 200
    assert "/merge/search" not in r.text
    assert "confirm=merge" not in r.text


# ---------- typeahead search ----------


def test_merge_search_finds_by_name(client, admin_on):
    mid = _add_maker(name="Zzz Source")
    target = _add_maker(name="Findable Target Co")
    r = client.get(f"/makers/{mid}/merge/search?q=findable")
    assert r.status_code == 200
    assert "Findable Target Co" in r.text
    assert f"/makers/{mid}?confirm=merge&into={target}" in r.text


def test_merge_search_ignores_short_queries(client, admin_on):
    mid = _add_maker(name="Selfsame Maker")
    _add_maker(name="Selsame Other")
    r = client.get(f"/makers/{mid}/merge/search?q=s")
    assert "Selsame Other" not in r.text


def test_merge_search_excludes_self(client, admin_on):
    mid = _add_maker(name="Selfsame Maker")
    r = client.get(f"/makers/{mid}/merge/search?q=selfsame")
    assert f"into={mid}" not in r.text


# ---------- confirm panel + commit ----------


def test_merge_confirm_panel_shows_impact(client, admin_on):
    a, b = _add_two_with_mention()
    r = client.get(f"/makers/{a}?confirm=merge&into={b}")
    assert r.status_code == 200
    assert "Confirm merge" in r.text
    assert f'action="/makers/{a}/merge"' in r.text
    assert "1 mention" in r.text


def test_merge_commits_moves_history_and_redirects_to_survivor(client, admin_on):
    a, b = _add_two_with_mention()
    r = client.post(f"/makers/{a}/merge", data={"into": str(b)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/makers/{b}"
    with session_scope() as session:
        assert session.get(Maker, a) is None  # source removed
        survivor = session.get(Maker, b)
        assert survivor is not None
        assert "Zzz Merge Source" in (survivor.aliases or [])  # name folded in
        assert session.query(Mention).filter_by(maker_id=b).count() == 1


def test_merge_self_blocked(client, admin_on):
    mid = _add_maker()
    r = client.get(f"/makers/{mid}?confirm=merge&into={mid}")
    assert r.status_code == 200
    assert "Can't merge" in r.text
    assert "Confirm merge" not in r.text
    r2 = client.post(f"/makers/{mid}/merge", data={"into": str(mid)}, follow_redirects=False)
    assert r2.status_code == 409


def test_merge_seed_source_blocked(client, admin_on):
    seed_id = _add_maker(name=SEED_NAME)
    target = _add_maker(name="Zzz Some Target")
    r = client.post(
        f"/makers/{seed_id}/merge", data={"into": str(target)}, follow_redirects=False
    )
    assert r.status_code == 409
    with session_scope() as session:
        assert session.get(Maker, seed_id) is not None  # not deleted


# ---------- near-duplicate suggestions on the detail page ----------


def test_detail_shows_merge_suggestions(client, admin_on):
    a, b = _add_two_similar_names()
    r = client.get(f"/makers/{a}")
    assert r.status_code == 200
    assert "Likely duplicates" in r.text
    assert f"/makers/{a}?confirm=merge&into={b}" in r.text
