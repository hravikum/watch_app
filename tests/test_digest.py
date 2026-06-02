"""Tests for the weekly "what changed" digest (chunk 3S).

Covers the shared compute function (windowing, the three sections, the
thin-history flag), the markdown renderer, the CLI command, and the web page.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from watch_app.api.routers.makers import router as makers_router
from watch_app.cli import app as cli_app
from watch_app.scoring.digest import (
    compute_weekly_digest,
    render_digest_markdown,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score, Source as SourceRow

NOW = datetime.now(timezone.utc)


def _src(s) -> SourceRow:
    src = SourceRow(name="DigestSrc", category="media", prestige_weight=0.5)
    s.add(src)
    s.flush()
    return src


# ---------- compute: new discoveries windowing ----------


def test_new_discoveries_respects_window(test_engine):
    with session_scope() as s:
        # "Zzz" names won't collide with the seed YAML, so they read as discovered.
        s.add(Maker(canonical_name="Zzz Recent", is_independent=True,
                    first_seen_at=NOW - timedelta(days=2)))
        s.add(Maker(canonical_name="Zzz Old", is_independent=True,
                    first_seen_at=NOW - timedelta(days=40)))

    with session_scope() as s:
        d = compute_weekly_digest(s, window_days=7)
    names = [m.canonical_name for m in d.new_discoveries]
    assert "Zzz Recent" in names
    assert "Zzz Old" not in names


# ---------- compute: movers passthrough + has_history ----------


def test_movers_and_history_flag(test_engine):
    with session_scope() as s:
        m = Maker(canonical_name="Zzz Climber", is_independent=True,
                  first_seen_at=NOW - timedelta(days=60))
        s.add(m)
        s.flush()
        # Historical rank 10 → current rank 2 = climbed 8 (>= min_change 3).
        s.add(Score(maker_id=m.id, preset="rising_star", value=2.0, rank=10,
                    computed_at=NOW - timedelta(days=10)))
        s.add(Score(maker_id=m.id, preset="rising_star", value=8.0, rank=2,
                    computed_at=NOW))

    with session_scope() as s:
        d = compute_weekly_digest(s, window_days=7, preset="rising_star")
    assert d.has_history is True
    assert "Zzz Climber" in [r["canonical_name"] for r in d.climbers]


def test_has_history_false_when_no_old_scores(test_engine):
    with session_scope() as s:
        m = Maker(canonical_name="Zzz Fresh", is_independent=True)
        s.add(m)
        s.flush()
        s.add(Score(maker_id=m.id, preset="rising_star", value=5.0, rank=1, computed_at=NOW))

    with session_scope() as s:
        d = compute_weekly_digest(s, window_days=7)
    assert d.has_history is False


# ---------- compute: watching activity ----------


def test_watching_includes_only_active_marked_makers(test_engine):
    with session_scope() as s:
        src = _src(s)
        # Watched + a new mention in window → included.
        active = Maker(canonical_name="Zzz Watched Active", is_independent=True,
                       relationship_state="watching",
                       first_seen_at=NOW - timedelta(days=90))
        # Watched but no activity → excluded.
        quiet = Maker(canonical_name="Zzz Watched Quiet", is_independent=True,
                      relationship_state="watching",
                      first_seen_at=NOW - timedelta(days=90))
        # Not watched but has a new mention → not in the watching section.
        unmarked = Maker(canonical_name="Zzz Unmarked", is_independent=True,
                         first_seen_at=NOW - timedelta(days=90))
        s.add_all([active, quiet, unmarked])
        s.flush()
        s.add(Mention(maker_id=active.id, source_id=src.id, url="https://d/1",
                      title="new", discovered_at=NOW - timedelta(days=1)))
        s.add(Mention(maker_id=unmarked.id, source_id=src.id, url="https://d/2",
                      title="new", discovered_at=NOW - timedelta(days=1)))
        # An OLD mention on the quiet maker — outside the window, so no activity.
        s.add(Mention(maker_id=quiet.id, source_id=src.id, url="https://d/3",
                      title="old", discovered_at=NOW - timedelta(days=40)))

    with session_scope() as s:
        d = compute_weekly_digest(s, window_days=7)
    names = [a.canonical_name for a in d.watching]
    assert "Zzz Watched Active" in names
    assert "Zzz Watched Quiet" not in names
    assert "Zzz Unmarked" not in names
    active_row = next(a for a in d.watching if a.canonical_name == "Zzz Watched Active")
    assert active_row.new_mentions == 1
    assert active_row.relationship_state == "watching"


def test_unknown_preset_raises(test_engine):
    with session_scope() as s:
        with pytest.raises(ValueError):
            compute_weekly_digest(s, preset="not_a_preset")


# ---------- markdown renderer ----------


def test_markdown_has_sections_and_names(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name="Zzz MdMaker", is_independent=True,
                    first_seen_at=NOW - timedelta(days=1)))

    with session_scope() as s:
        md = render_digest_markdown(compute_weekly_digest(s, window_days=7))
    assert "# What changed" in md
    assert "## New discoveries" in md
    assert "## On your list" in md
    assert "Zzz MdMaker" in md


def test_markdown_empty_state(test_engine):
    with session_scope() as s:
        md = render_digest_markdown(compute_weekly_digest(s, window_days=7))
    assert "Nothing surfaced" in md


# ---------- CLI ----------


def test_cli_digest_runs(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name="Zzz CliMaker", is_independent=True,
                    first_seen_at=NOW - timedelta(days=1)))
    runner = CliRunner()
    res = runner.invoke(cli_app, ["digest", "--days", "7"])
    assert res.exit_code == 0
    assert "What changed" in res.stdout


def test_cli_digest_markdown(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name="Zzz CliMd", is_independent=True,
                    first_seen_at=NOW - timedelta(days=1)))
    runner = CliRunner()
    res = runner.invoke(cli_app, ["digest", "--markdown"])
    assert res.exit_code == 0
    assert "# What changed" in res.stdout
    assert "Zzz CliMd" in res.stdout


def test_cli_digest_unknown_preset_exits_nonzero(test_engine):
    runner = CliRunner()
    res = runner.invoke(cli_app, ["digest", "--preset", "bogus"])
    assert res.exit_code != 0


# ---------- web ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def test_digest_page_renders(client, test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name="Zzz PageMaker", is_independent=True,
                    first_seen_at=NOW - timedelta(days=1)))
    r = client.get("/digest")
    assert r.status_code == 200
    assert "What changed" in r.text
    assert "Zzz PageMaker" in r.text


def test_digest_page_validates_inputs(client, test_engine):
    # Unknown preset/days fall back to defaults rather than erroring.
    r = client.get("/digest?preset=bogus&days=999")
    assert r.status_code == 200
