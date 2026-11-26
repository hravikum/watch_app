"""Tests for renaming a maker whose name a scan grabbed wrong (chunk 4L).

Covers the rename_maker operation (dry-run/commit, the old-name alias and its
opt-out, own-alias promotion, the seed + collision blockers, and that the old
name then resolves to the renamed maker), the review-grid wiring (the Name
field renders, a changed name renames on Save, a blocker is a 409 with nothing
saved, an unchanged name is a no-op), and the `rename` CLI.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from watch_app.api.routers.makers import router as makers_router
from watch_app.cli import app as cli_app
from watch_app.config import get_settings
from watch_app.extraction.maker_admin import rename_maker
from watch_app.extraction.resolver import resolve_or_create_maker
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Source


SEED_NAME = "Simon Brette"  # present in the seed YAML
BAD = "Introducing Zzz Renamed"
GOOD = "Zzz Renamed"


def _add(name: str = BAD, **kwargs) -> int:
    with session_scope() as s:
        m = Maker(canonical_name=name, is_independent=True, **kwargs)
        s.add(m)
        s.flush()
        return m.id


def _get(mid: int) -> Maker:
    with session_scope() as s:
        return s.get(Maker, mid)


# ---------- rename_maker ----------


def test_rename_commit_updates_name_and_keeps_old_as_alias(test_engine):
    mid = _add(aliases=["Zzz Other Spelling"])
    with session_scope() as s:
        res = rename_maker(s, str(mid), GOOD, commit=True)
    assert res.done and res.alias_kept and not res.blockers
    m = _get(mid)
    assert m.canonical_name == GOOD
    assert m.aliases == ["Zzz Other Spelling", BAD]


def test_rename_dry_run_changes_nothing(test_engine):
    mid = _add()
    with session_scope() as s:
        res = rename_maker(s, str(mid), GOOD)
    assert not res.done and res.alias_kept  # previewed, not applied
    m = _get(mid)
    assert m.canonical_name == BAD
    assert not m.aliases


def test_rename_without_keep_alias_drops_old_name(test_engine):
    mid = _add()
    with session_scope() as s:
        res = rename_maker(s, str(mid), GOOD, keep_old_as_alias=False, commit=True)
    assert res.done and not res.alias_kept
    m = _get(mid)
    assert m.canonical_name == GOOD
    assert not m.aliases


def test_rename_to_own_alias_promotes_it(test_engine):
    mid = _add(aliases=[GOOD, "Zzz Keep Me"])
    with session_scope() as s:
        res = rename_maker(s, str(mid), GOOD, commit=True)
    assert res.alias_promoted
    m = _get(mid)
    assert m.canonical_name == GOOD
    assert m.aliases == ["Zzz Keep Me", BAD]


def test_rename_normalization_only_change_adds_no_alias(test_engine):
    mid = _add(name="zzz renamed")
    with session_scope() as s:
        res = rename_maker(s, str(mid), GOOD, commit=True)
    assert res.done and not res.alias_kept
    m = _get(mid)
    assert m.canonical_name == GOOD
    assert not m.aliases


def test_rename_by_exact_name_ref(test_engine):
    mid = _add()
    with session_scope() as s:
        rename_maker(s, BAD, GOOD, commit=True)
    assert _get(mid).canonical_name == GOOD


def test_rename_blocked_when_name_matches_other_makers_canonical(test_engine):
    other = _add(name="Zzz Collide")
    mid = _add()
    with session_scope() as s:
        # Normalizes to the other maker's name (generic "Atelier" prefix stripped).
        res = rename_maker(s, str(mid), "Atelier ZZZ Collide", commit=True)
    assert res.blockers and "Zzz Collide" in res.blockers[0]
    assert f"id {other}" in res.blockers[0]
    assert "Merge" in res.blockers[0]
    assert _get(mid).canonical_name == BAD


def test_rename_blocked_when_name_matches_other_makers_alias(test_engine):
    _add(name="Zzz Holder", aliases=[GOOD])
    mid = _add()
    with session_scope() as s:
        res = rename_maker(s, str(mid), GOOD, commit=True)
    assert res.blockers and "Zzz Holder" in res.blockers[0]
    assert _get(mid).canonical_name == BAD


def test_rename_blocked_for_seed_maker(test_engine):
    mid = _add(name=SEED_NAME)
    with session_scope() as s:
        res = rename_maker(s, str(mid), "Zzz Not Seed", commit=True)
    assert res.blockers and "seed maker" in res.blockers[0]
    assert _get(mid).canonical_name == SEED_NAME


@pytest.mark.parametrize("new_name", ["", "   ", BAD])
def test_rename_blocked_for_empty_or_unchanged_name(test_engine, new_name):
    mid = _add()
    with session_scope() as s:
        res = rename_maker(s, str(mid), new_name, commit=True)
    assert res.blockers and not res.done
    assert _get(mid).canonical_name == BAD


def test_rename_unknown_maker_reports_blocker(test_engine):
    with session_scope() as s:
        res = rename_maker(s, "99999", GOOD, commit=True)
    assert res.blockers and "No maker found" in res.blockers[0]


def test_rename_keeps_mentions_attached(test_engine):
    with session_scope() as s:
        src = Source(name="RenameSrc", category="media", prestige_weight=0.5)
        m = Maker(canonical_name=BAD, is_independent=True)
        s.add_all([src, m])
        s.flush()
        s.add(Mention(maker_id=m.id, source_id=src.id, url="http://x/1", title="t"))
        mid = m.id
    with session_scope() as s:
        rename_maker(s, str(mid), GOOD, commit=True)
    with session_scope() as s:
        assert s.query(Mention).filter_by(maker_id=mid).count() == 1


# A junk name that does NOT fuzzy-match GOOD on its own (WRatio ~30), so these
# two tests show the alias is what routes it — not the resolver's fuzzy step.
JUNK = "Qqq Junk Heading"


def test_old_name_resolves_to_renamed_maker_when_kept(test_engine):
    mid = _add(name=JUNK)
    with session_scope() as s:
        rename_maker(s, str(mid), GOOD, commit=True)
    with session_scope() as s:
        assert resolve_or_create_maker(s, JUNK).id == mid
        assert s.query(Maker).count() == 1


def test_old_name_recreated_when_dropped(test_engine):
    mid = _add(name=JUNK)
    with session_scope() as s:
        rename_maker(s, str(mid), GOOD, keep_old_as_alias=False, commit=True)
    with session_scope() as s:
        assert resolve_or_create_maker(s, JUNK).id != mid
        assert s.query(Maker).count() == 2


# ---------- review grid ----------


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


def test_review_renders_name_field(client, admin_on):
    _add()
    r = client.get("/review")
    assert r.status_code == 200
    assert 'name="canonical_name"' in r.text
    assert f'value="{BAD}"' in r.text
    assert 'name="keep_old_name"' in r.text


def test_review_save_renames_and_keeps_alias(client, admin_on):
    mid = _add()
    r = client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": GOOD, "keep_old_name": "on", "country": "Japan"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/review"
    m = _get(mid)
    assert m.canonical_name == GOOD
    assert m.aliases == [BAD]
    assert m.country == "Japan"  # the rest of the card still saves


def test_review_save_rename_unticked_drops_old_name(client, admin_on):
    mid = _add()
    r = client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": GOOD},  # checkbox omitted → unticked
        follow_redirects=False,
    )
    assert r.status_code == 303
    m = _get(mid)
    assert m.canonical_name == GOOD
    assert not m.aliases


def test_review_save_rename_collision_is_409_and_saves_nothing(client, admin_on):
    _add(name=GOOD)
    mid = _add()
    r = client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": GOOD, "keep_old_name": "on", "country": "Japan"},
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert "Merge" in r.json()["detail"]
    m = _get(mid)
    assert m.canonical_name == BAD
    assert m.country is None


def test_review_save_unchanged_name_is_noop(client, admin_on):
    mid = _add()
    r = client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": BAD, "keep_old_name": "on", "is_independent": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    m = _get(mid)
    assert m.canonical_name == BAD
    assert not m.aliases


# ---------- CLI ----------


def test_cli_rename_dry_run_then_commit(test_engine):
    mid = _add()
    runner = CliRunner()
    res = runner.invoke(cli_app, ["rename", str(mid), GOOD])
    assert res.exit_code == 0
    assert "Dry run" in res.stdout
    assert _get(mid).canonical_name == BAD

    res = runner.invoke(cli_app, ["rename", str(mid), GOOD, "--drop-old-name", "--commit"])
    assert res.exit_code == 0
    assert "Renamed" in res.stdout
    m = _get(mid)
    assert m.canonical_name == GOOD
    assert not m.aliases


def test_cli_rename_blocker_exits_nonzero(test_engine):
    mid = _add(name=SEED_NAME)
    res = CliRunner().invoke(cli_app, ["rename", str(mid), GOOD, "--commit"])
    assert res.exit_code == 1
    assert "seed maker" in res.stdout
