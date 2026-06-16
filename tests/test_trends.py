"""Tests for the /trends page (time-series charts).

Covers three layers:
  1. fetch_mention_volume   — awards excluded, gap-filled daily buckets
  2. fetch_rank_trajectories — current top-N, full stored rank history
  3. _build_volume_chart / _build_trajectory_chart — pure SVG geometry,
     including the divide-by-zero edges (single day, single snapshot)
  4. the /trends route — render, preset switch, fallback, empty state, nav
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import (
    router as makers_router,
    _build_volume_chart,
    _build_trajectory_chart,
)
from watch_app.scoring.queries import fetch_mention_volume, fetch_rank_trajectories
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score, Source as SourceRow


def _seed_trends_population():
    """Two media makers with rank history, plus an awards mention to exclude.

    - Media source 'SJX', awards source 'GPHG'.
    - Alpha: rank history 9 → 5 → 2 (a climber); current #2
    - Beta:  rank history 3 → 4 → 1 ; current #1
    - Media mentions land on three distinct ingest days; one GPHG (awards)
      mention with an old published_at must NOT count toward volume.
    """
    with session_scope() as session:
        media = SourceRow(name="SJX", category="media", prestige_weight=1.0)
        awards = SourceRow(name="GPHG", category="awards", prestige_weight=1.0)
        session.add_all([media, awards])
        session.flush()

        alpha = Maker(canonical_name="Alpha", country="CH", is_independent=True)
        beta = Maker(canonical_name="Beta", country="DE", is_independent=True)
        session.add_all([alpha, beta])
        session.flush()

        now = datetime.now(timezone.utc)
        # Media mentions across three ingest days (2 on day-3, 1 on day-2, 1 today).
        media_days = [3, 3, 2, 0]
        for i, d in enumerate(media_days):
            session.add(Mention(
                maker_id=alpha.id, source_id=media.id,
                url=f"http://x.com/{i}", title="t",
                discovered_at=now - timedelta(days=d),
            ))
        # Awards mention with a historical publish date — excluded from volume.
        session.add(Mention(
            maker_id=beta.id, source_id=awards.id,
            url="http://gphg/1", title="award",
            published_at=datetime(2023, 11, 1),
            discovered_at=now - timedelta(days=1),
        ))

        # Score history (older → newer): (days_ago, alpha_rank, beta_rank)
        for d, ra, rb in [(20, 9, 3), (10, 5, 4), (0, 2, 1)]:
            session.add(Score(maker_id=alpha.id, preset="rising_star",
                              value=1.0, rank=ra, computed_at=now - timedelta(days=d)))
            session.add(Score(maker_id=beta.id, preset="rising_star",
                              value=1.0, rank=rb, computed_at=now - timedelta(days=d)))


# ---------- fetch_mention_volume ----------


def test_volume_excludes_awards(test_engine):
    """Awards-category mentions are not counted in the volume series."""
    _seed_trends_population()
    with session_scope() as session:
        vol = fetch_mention_volume(session)
    total = sum(v["count"] for v in vol)
    assert total == 4  # the four media mentions, not the GPHG one


def test_volume_is_gap_filled(test_engine):
    """Days with no mentions appear as zero-count points, not gaps."""
    _seed_trends_population()
    with session_scope() as session:
        vol = fetch_mention_volume(session)
    # Span is day-3 .. today (4 calendar days inclusive), one of which (day-1)
    # has no media mention and must show as a zero.
    assert len(vol) == 4
    assert any(v["count"] == 0 for v in vol)
    # Continuous: each date is exactly one day after the previous.
    for earlier, later in zip(vol, vol[1:]):
        assert later["date"] - earlier["date"] == timedelta(days=1)


def test_volume_empty_when_no_mentions(test_engine):
    """No non-awards mentions → empty list (not an error)."""
    with session_scope() as session:
        assert fetch_mention_volume(session) == []


def test_volume_empty_when_only_awards(test_engine):
    """A DB with only awards mentions yields an empty volume series."""
    with session_scope() as session:
        awards = SourceRow(name="GPHG", category="awards", prestige_weight=1.0)
        session.add(awards)
        session.flush()
        m = Maker(canonical_name="Only Awards", is_independent=True)
        session.add(m)
        session.flush()
        session.add(Mention(maker_id=m.id, source_id=awards.id,
                            url="http://gphg/x", title="a",
                            discovered_at=datetime.now(timezone.utc)))
    with session_scope() as session:
        assert fetch_mention_volume(session) == []


# ---------- fetch_rank_trajectories ----------


def test_trajectories_selects_current_top(test_engine):
    """Makers are the current top-N, ordered by current rank ascending."""
    _seed_trends_population()
    with session_scope() as session:
        traj = fetch_rank_trajectories(session, "rising_star", top_n=10)
    names = [m["canonical_name"] for m in traj["makers"]]
    assert names == ["Beta", "Alpha"]  # Beta #1, Alpha #2


def test_trajectories_carry_full_history(test_engine):
    """Each maker's points are their full stored rank history, time-ordered."""
    _seed_trends_population()
    with session_scope() as session:
        traj = fetch_rank_trajectories(session, "rising_star", top_n=10)
    alpha = next(m for m in traj["makers"] if m["canonical_name"] == "Alpha")
    assert [p["rank"] for p in alpha["points"]] == [9, 5, 2]


def test_trajectories_max_rank_is_worst_seen(test_engine):
    """max_rank is the largest rank across all plotted points (y-axis floor)."""
    _seed_trends_population()
    with session_scope() as session:
        traj = fetch_rank_trajectories(session, "rising_star", top_n=10)
    assert traj["max_rank"] == 9  # Alpha's worst historical rank


def test_trajectories_respects_top_n(test_engine):
    """top_n caps how many makers come back."""
    _seed_trends_population()
    with session_scope() as session:
        traj = fetch_rank_trajectories(session, "rising_star", top_n=1)
    assert len(traj["makers"]) == 1
    assert traj["makers"][0]["canonical_name"] == "Beta"


def test_trajectories_empty_when_no_scores(test_engine):
    """No scores under the preset → empty structure, no error."""
    with session_scope() as session:
        traj = fetch_rank_trajectories(session, "rising_star", top_n=10)
    assert traj == {"makers": [], "max_rank": 0}


# ---------- geometry: _build_volume_chart ----------


def test_build_volume_chart_none_on_empty():
    assert _build_volume_chart([]) is None


def test_build_volume_chart_single_day_no_div_zero():
    """A one-day series must not divide by zero on the x-scale."""
    chart = _build_volume_chart([{"date": date(2026, 6, 1), "count": 5}])
    assert chart is not None
    assert chart["line"]  # a coordinate string exists
    assert chart["area"].startswith("M ")


def test_build_volume_chart_all_zero_no_div_zero():
    """All-zero counts must not divide by zero on the y-scale."""
    series = [{"date": date(2026, 6, 1) + timedelta(days=i), "count": 0} for i in range(3)]
    chart = _build_volume_chart(series)
    assert chart is not None
    assert chart["y_max"] == 1  # floored


# ---------- geometry: _build_trajectory_chart ----------


def test_build_trajectory_chart_none_when_no_points():
    assert _build_trajectory_chart({"makers": [], "max_rank": 0}) is None
    assert _build_trajectory_chart(
        {"makers": [{"maker_id": 1, "canonical_name": "X",
                     "current_rank": 1, "points": []}], "max_rank": 0}
    ) is None


def test_build_trajectory_chart_single_snapshot_no_div_zero():
    """One snapshot for one maker: zero time-span and max_rank=1 are safe."""
    t = datetime(2026, 6, 1, 12, 0, 0)
    chart = _build_trajectory_chart({
        "makers": [{
            "maker_id": 1, "canonical_name": "Solo", "current_rank": 1,
            "points": [{"computed_at": t, "rank": 1}],
        }],
        "max_rank": 1,
    })
    assert chart is not None
    assert len(chart["lines"]) == 1
    line = chart["lines"][0]
    assert line["poly"]  # has at least the one coordinate
    assert "end_x" in line and "end_y" in line


# ---------- route ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def test_trends_page_renders(client, test_engine):
    _seed_trends_population()
    r = client.get("/trends")
    assert r.status_code == 200
    assert "Trends" in r.text
    assert "Mention volume" in r.text
    assert "Rank trajectories" in r.text
    assert "<svg" in r.text


def test_trends_volume_excludes_awards_in_header(client, test_engine):
    """The volume header reflects the non-awards count."""
    _seed_trends_population()
    r = client.get("/trends")
    assert "4 over" in r.text  # four media mentions


def test_trends_legend_lists_top_maker(client, test_engine):
    """The trajectory legend links the current #1 maker to its detail page."""
    _seed_trends_population()
    r = client.get("/trends")
    assert ">Beta</a>" in r.text
    assert "/makers/" in r.text


def test_trends_preset_switch(client, test_engine):
    """A different preset renders its own trajectories."""
    _seed_trends_population()
    with session_scope() as session:
        beta = session.query(Maker).filter_by(canonical_name="Beta").one()
        now = datetime.now(timezone.utc)
        session.add(Score(maker_id=beta.id, preset="hidden_gem",
                          value=2.0, rank=1, computed_at=now))
    r = client.get("/trends?preset=hidden_gem")
    assert r.status_code == 200
    assert "hidden gem" in r.text


def test_trends_invalid_preset_falls_back(client, test_engine):
    _seed_trends_population()
    r = client.get("/trends?preset=__nope__")
    assert r.status_code == 200
    # default preset (rising_star) still renders the trajectory section
    assert "Rank trajectories" in r.text


def test_trends_empty_state(client, test_engine):
    """No data at all → friendly empty states, still 200."""
    r = client.get("/trends")
    assert r.status_code == 200
    assert "No mention data yet" in r.text
    assert "No ranked makers" in r.text


def test_trends_nav_link_present(client, test_engine):
    r = client.get("/")
    assert r.status_code == 200
    assert "/trends" in r.text
    assert "Trends" in r.text
