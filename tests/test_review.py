"""Tests for the admin review/edit page (chunk 3H).

Covers:
  1. Admin gating — /review and its POST 404 when WATCH_APP_ADMIN is off.
  2. Rendering — the page lists discovered makers and the taste vocabulary.
  3. Saving — edits persist, "auto" markers clear, taste_enriched_at stamps.
  4. Overwrite semantics — emptying a field clears it (human is authoritative).
  5. Validation — out-of-vocabulary values are rejected.
  6. Seed makers are excluded.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.config import get_settings
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


DISCOVERED = "Zzz Review Test Brand"
SEED_NAME = "Simon Brette"  # present in the seed YAML


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


def _add(**kwargs) -> int:
    with session_scope() as session:
        m = Maker(**kwargs)
        session.add(m)
        session.flush()
        return m.id


# ---------- Gating ----------


def test_review_404_when_admin_off(client):
    assert client.get("/review").status_code == 404


def test_review_post_404_when_admin_off(client):
    _add(canonical_name=DISCOVERED, is_independent=True)
    r = client.post("/review/maker/1", data={"maker_type": "solo-watchmaker"})
    assert r.status_code == 404


# ---------- Rendering ----------


def test_review_renders_discovered_and_vocab(client, admin_on):
    _add(canonical_name=DISCOVERED, is_independent=True)
    r = client.get("/review")
    assert r.status_code == 200
    assert DISCOVERED in r.text
    # Vocabulary options are rendered (display label for solo-watchmaker).
    assert "Solo watchmaker" in r.text


def test_review_excludes_seed_makers(client, admin_on):
    _add(canonical_name=SEED_NAME, is_independent=True)
    r = client.get("/review")
    assert r.status_code == 200
    assert SEED_NAME not in r.text


# ---------- Saving ----------


def test_save_writes_tags_and_clears_auto(client, admin_on):
    mid = _add(
        canonical_name=DISCOVERED,
        is_independent=True,
        maker_type="microbrand",
        enriched_fields=["maker_type", "aesthetic_tags"],
    )
    r = client.post(
        f"/review/maker/{mid}",
        data={
            "aesthetic_tags": ["modern", "tool"],
            "maker_type": "solo-watchmaker",
            "price_band": "high",
            "is_independent": "on",
            "country": "United States",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=mid).one()
        assert m.aesthetic_tags == ["modern", "tool"]
        assert m.maker_type == "solo-watchmaker"   # overwrote microbrand
        assert m.price_band == "high"
        assert m.country == "United States"
        assert m.is_independent is True
        assert m.taste_enriched_at is not None
        # Edited fields are now human-reviewed → auto markers gone.
        assert not m.enriched_fields


def test_save_empty_clears_field(client, admin_on):
    mid = _add(
        canonical_name=DISCOVERED,
        is_independent=True,
        maker_type="solo-watchmaker",
        aesthetic_tags=["modern"],
    )
    # Submit with maker_type blank and no aesthetic checkboxes → both cleared.
    r = client.post(
        f"/review/maker/{mid}",
        data={"maker_type": "", "is_independent": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=mid).one()
        assert m.maker_type is None
        assert m.aesthetic_tags is None


def test_save_unchecking_independent_sets_false(client, admin_on):
    mid = _add(canonical_name=DISCOVERED, is_independent=True)
    # is_independent checkbox omitted → unchecked → False.
    r = client.post(
        f"/review/maker/{mid}", data={"maker_type": ""}, follow_redirects=False
    )
    assert r.status_code == 303
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=mid).one()
        assert m.is_independent is False


def test_save_rejects_out_of_vocab(client, admin_on):
    mid = _add(canonical_name=DISCOVERED, is_independent=True)
    r = client.post(
        f"/review/maker/{mid}",
        data={"maker_type": "not-a-real-type"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=mid).one()
        assert m.maker_type is None  # nothing written


def test_save_404_for_missing_maker(client, admin_on):
    r = client.post(
        "/review/maker/99999", data={"maker_type": "solo-watchmaker"},
        follow_redirects=False,
    )
    assert r.status_code == 404
