"""Tests for promote-to-seed (chunk 3L).

Serialization and the collision/advisory checks run against the real committed
seed (read-only). The commit, idempotency, and route tests redirect the seed
path to a temp copy so the real known_makers.yaml is never mutated.
"""
from __future__ import annotations

import shutil

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.config import get_settings
from watch_app.extraction import promote
from watch_app.extraction.promote import (
    build_entry_lines,
    check_promotable,
    promote_to_seed,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


# ---------- build_entry_lines ----------


def test_build_entry_format():
    m = Maker(
        canonical_name="Zzz Atelier",
        aesthetic_tags=["classical", "modern"],
        maker_type="solo-watchmaker",
        complication_focus=["time-only"],
        country="France",
        founded_year=2021,
        aliases=["Zzz"],
    )
    text = "".join(build_entry_lines(m))
    assert '- canonical_name: "Zzz Atelier"' in text
    assert 'aesthetic_tags: ["classical", "modern"]' in text  # flow-style list
    assert "founded_year: 2021" in text  # bare int, not quoted
    assert "price_band" not in text  # null dimension omitted
    assert "is_independent" not in text  # true (default) → omitted


def test_build_entry_non_independent_written():
    m = Maker(canonical_name="Zzz Group", maker_type="established-indie", is_independent=False)
    assert "is_independent: false" in "".join(build_entry_lines(m))


# ---------- check_promotable (real seed, read-only) ----------


def test_check_clean_tagged_maker():
    r = check_promotable(Maker(canonical_name="Zzz Brand New", maker_type="solo-watchmaker"))
    assert not r.blockers and not r.needs_confirm and not r.warnings


def test_check_untagged_warns():
    r = check_promotable(Maker(canonical_name="Zzz Untagged Maker"))
    assert r.warnings and not r.blockers


def test_check_exact_collision_blocks():
    # "Simon Brette" is a real seed entry.
    r = check_promotable(Maker(canonical_name="Simon Brette", maker_type="solo-watchmaker"))
    assert r.blockers


def test_check_fuzzy_near_match_needs_confirm():
    r = check_promotable(Maker(canonical_name="Simon Brett", maker_type="solo-watchmaker"))
    assert r.needs_confirm and not r.blockers


# ---------- commit / append (temp seed copy) ----------


@pytest.fixture
def tmp_seed(tmp_path, monkeypatch):
    dest = tmp_path / "known_makers.yaml"
    shutil.copy(promote.SEED_PATH, dest)
    monkeypatch.setattr("watch_app.extraction.seed.SEED_PATH", dest)
    monkeypatch.setattr("watch_app.extraction.promote.SEED_PATH", dest)
    return dest


def test_dry_run_writes_nothing(tmp_seed):
    before = tmp_seed.read_text()
    r = promote_to_seed(None, Maker(canonical_name="Zzz Dry Run", maker_type="solo-watchmaker"))
    assert r.written is False
    assert tmp_seed.read_text() == before


def test_commit_appends_and_parses(tmp_seed):
    m = Maker(
        canonical_name="Zzz Committed",
        aesthetic_tags=["classical"],
        maker_type="solo-watchmaker",
        complication_focus=["time-only"],
    )
    r = promote_to_seed(None, m, commit=True)
    assert r.written is True
    data = yaml.safe_load(tmp_seed.read_text())
    entry = next(e for e in data["makers"] if e["canonical_name"] == "Zzz Committed")
    assert entry["aesthetic_tags"] == ["classical"]
    assert "# --- Promoted from discovery ---" in tmp_seed.read_text()


def test_repromote_is_blocked(tmp_seed):
    m = Maker(canonical_name="Zzz Twice", maker_type="solo-watchmaker")
    assert promote_to_seed(None, m, commit=True).written is True
    # Now present in the (temp) seed → exact collision blocks a second promote.
    again = promote_to_seed(None, m, commit=True)
    assert again.written is False
    assert again.blockers


def test_needs_confirm_requires_force(tmp_seed):
    m = Maker(canonical_name="Simon Brett", maker_type="solo-watchmaker")
    assert promote_to_seed(None, m, commit=True).written is False  # near-match, no force
    assert promote_to_seed(None, m, commit=True, force=True).written is True


# ---------- route ----------


@pytest.fixture
def admin_client(test_engine, monkeypatch):
    monkeypatch.setenv("WATCH_APP_ADMIN", "1")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(makers_router)
    yield TestClient(app)
    get_settings.cache_clear()


def test_route_promotes_discovered(admin_client, tmp_seed):
    with session_scope() as s:
        m = Maker(canonical_name="Zzz Route Maker", maker_type="solo-watchmaker")
        s.add(m)
        s.flush()
        mid = m.id
    r = admin_client.post(f"/review/promote/{mid}", follow_redirects=False)
    assert r.status_code == 303
    names = [e["canonical_name"] for e in yaml.safe_load(tmp_seed.read_text())["makers"]]
    assert "Zzz Route Maker" in names


def test_route_collision_returns_409(admin_client, tmp_seed):
    with session_scope() as s:
        m = Maker(canonical_name="Simon Brette", maker_type="solo-watchmaker")
        s.add(m)
        s.flush()
        mid = m.id
    r = admin_client.post(f"/review/promote/{mid}", follow_redirects=False)
    assert r.status_code == 409


def test_route_404_when_admin_off(test_engine):
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(makers_router)
    client = TestClient(app)
    with session_scope() as s:
        m = Maker(canonical_name="Zzz Hidden", maker_type="solo-watchmaker")
        s.add(m)
        s.flush()
        mid = m.id
    assert client.post(f"/review/promote/{mid}", follow_redirects=False).status_code == 404
