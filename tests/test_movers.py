"""Tests for the /movers page (rank-change tracking).

Verifies:
  1. fetch_rank_changes correctly classifies climbers, fallers, and new entrants
  2. The min_change threshold filters out small shuffles
  3. Makers without prior scores in the window are flagged as new entrants
  4. The route validates preset and days against allow-lists
  5. The page renders movers in the correct visual sections
  6. Empty-history case (no scores N days ago) is handled gracefully
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.scoring.queries import fetch_rank_changes
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score, Source as SourceRow


def _seed_movement_population():
    """Build a population with explicit historical scores so we can assert
    on rank changes deterministically.

    Six makers, all under preset='rising_star':
      - Climber:       was rank 20 → now rank 5  (climbed 15)
      - Climber Edge:  was rank 8  → now rank 5  (climbed 3, exactly at threshold)
      - Faller:        was rank 5  → now rank 25 (fell 20)
      - Stable:        was rank 10 → now rank 11 (changed 1, below threshold)
      - New Entrant:   no prior score → now rank 7
      - Just Below:    was rank 15 → now rank 17 (changed 2, below threshold)
    """
    with session_scope() as session:
        src = SourceRow(name="Test", category="media", prestige_weight=1.0)
        session.add(src)
        session.flush()

        names = ["Climber", "Climber Edge", "Faller", "Stable",
                 "New Entrant", "Just Below"]
        makers = []
        for n in names:
            m = Maker(canonical_name=n, country="X", is_independent=True)
            session.add(m)
            makers.append(m)
        session.flush()

        # Add a single mention to each so mention_count is non-zero
        for m in makers:
            session.add(Mention(
                maker_id=m.id, source_id=src.id,
                url=f"http://x.com/{m.canonical_name}",
                title="x", discovered_at=datetime.now(timezone.utc),
            ))

        now = datetime.now(timezone.utc)
        old = now - timedelta(days=10)  # historical
        recent = now - timedelta(minutes=5)  # current

        # Historical scores at `old` (10 days ago — outside the default 7-day window).
        historical = [
            (makers[0], 20, 0.5),  # Climber
            (makers[1], 8, 1.5),   # Climber Edge
            (makers[2], 5, 2.0),   # Faller
            (makers[3], 10, 1.0),  # Stable
            # makers[4] (New Entrant) has no historical score
            (makers[5], 15, 0.7),  # Just Below
        ]
        for m, rank, val in historical:
            session.add(Score(
                maker_id=m.id, preset="rising_star",
                value=val, rank=rank, computed_at=old,
            ))

        # Current scores
        current = [
            (makers[0], 5, 2.5),    # Climber: 20 → 5
            (makers[1], 5, 2.5),    # Climber Edge: 8 → 5 (rank tie OK in test)
            (makers[2], 25, 0.1),   # Faller: 5 → 25
            (makers[3], 11, 0.95),  # Stable: 10 → 11
            (makers[4], 7, 1.8),    # New Entrant: no prior → 7
            (makers[5], 17, 0.6),   # Just Below: 15 → 17
        ]
        for m, rank, val in current:
            session.add(Score(
                maker_id=m.id, preset="rising_star",
                value=val, rank=rank, computed_at=recent,
            ))


# ---------- Helper tests ----------


def test_climbers_appear_in_climbers(test_engine):
    """Makers whose rank improved by >= min_change appear under climbers."""
    _seed_movement_population()
    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7)
    names = {m["canonical_name"] for m in groups["climbers"]}
    assert "Climber" in names
    assert "Climber Edge" in names  # exactly at threshold


def test_fallers_appear_in_fallers(test_engine):
    """Makers whose rank worsened by >= min_change appear under fallers."""
    _seed_movement_population()
    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7)
    names = {m["canonical_name"] for m in groups["fallers"]}
    assert "Faller" in names


def test_below_threshold_excluded(test_engine):
    """Small rank shuffles are filtered out — neither climber nor faller."""
    _seed_movement_population()
    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7)
    all_movement_names = (
        {m["canonical_name"] for m in groups["climbers"]}
        | {m["canonical_name"] for m in groups["fallers"]}
        | {m["canonical_name"] for m in groups["new_entrants"]}
    )
    assert "Stable" not in all_movement_names
    assert "Just Below" not in all_movement_names


def test_new_entrants_have_no_prior_rank(test_engine):
    """A maker with no score at the cutoff window appears as a new entrant."""
    _seed_movement_population()
    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7)
    names = {m["canonical_name"] for m in groups["new_entrants"]}
    assert "New Entrant" in names
    new_entrant = next(
        m for m in groups["new_entrants"] if m["canonical_name"] == "New Entrant"
    )
    assert new_entrant["prior_rank"] is None
    assert new_entrant["rank_change"] is None


def test_climbers_sorted_by_movement_descending(test_engine):
    """Climbers sorted by largest gain first."""
    _seed_movement_population()
    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7)
    rank_changes = [m["rank_change"] for m in groups["climbers"]]
    assert rank_changes == sorted(rank_changes, reverse=True)


def test_rank_change_value_is_correct(test_engine):
    """rank_change = prior_rank - current_rank (positive = climbed)."""
    _seed_movement_population()
    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7)
    climber = next(m for m in groups["climbers"] if m["canonical_name"] == "Climber")
    assert climber["prior_rank"] == 20
    assert climber["current_rank"] == 5
    assert climber["rank_change"] == 15

    faller = next(m for m in groups["fallers"] if m["canonical_name"] == "Faller")
    assert faller["prior_rank"] == 5
    assert faller["current_rank"] == 25
    assert faller["rank_change"] == -20


def test_min_change_parameterizable(test_engine):
    """A higher threshold filters out moderate movements."""
    _seed_movement_population()
    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7, min_change=10)
    names = {m["canonical_name"] for m in groups["climbers"]}
    # Climber (15) qualifies, Climber Edge (3) does not at this threshold
    assert "Climber" in names
    assert "Climber Edge" not in names


def test_days_window_clamped(test_engine):
    """Pathological days values are clamped to a sensible range."""
    _seed_movement_population()
    with session_scope() as session:
        # Negative or zero — should clamp to >= 1 and not error
        groups = fetch_rank_changes(session, "rising_star", days=-5)
        assert "climbers" in groups
        # Way too large — should clamp to max
        groups = fetch_rank_changes(session, "rising_star", days=999)
        assert "climbers" in groups


def test_empty_db_returns_empty_groups(test_engine):
    """No scores at all → all three groups are empty, no error."""
    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7)
    assert groups == {"climbers": [], "fallers": [], "new_entrants": []}


def test_no_history_means_all_new_entrants(test_engine):
    """If every score in the DB is recent (within the window), every
    ranked maker shows up as a new entrant. Correct behavior for a
    fresh installation."""
    with session_scope() as session:
        src = SourceRow(name="T", category="media", prestige_weight=1.0)
        session.add(src)
        session.flush()
        m = Maker(canonical_name="Fresh", country="X", is_independent=True)
        session.add(m)
        session.flush()
        recent = datetime.now(timezone.utc)
        session.add(Score(
            maker_id=m.id, preset="rising_star", value=1.0, rank=1,
            computed_at=recent,
        ))

    with session_scope() as session:
        groups = fetch_rank_changes(session, "rising_star", days=7)
    assert len(groups["new_entrants"]) == 1
    assert groups["climbers"] == []
    assert groups["fallers"] == []


# ---------- Route tests ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def test_movers_page_renders(client, test_engine):
    """Default page loads with climbers/fallers/new entrants visible."""
    _seed_movement_population()
    r = client.get("/movers")
    assert r.status_code == 200
    assert "Movers" in r.text
    assert "Climbers" in r.text
    assert "Fallers" in r.text
    assert "New entrants" in r.text


def test_movers_page_includes_correct_makers(client, test_engine):
    """Specific makers appear under their correct sections in HTML."""
    _seed_movement_population()
    r = client.get("/movers")
    text = r.text
    # All three movement makers appear
    assert ">Climber</a>" in text
    assert ">Faller</a>" in text
    assert ">New Entrant</a>" in text
    # Below-threshold makers don't appear
    assert ">Stable</a>" not in text
    assert ">Just Below</a>" not in text


def test_movers_page_invalid_preset_falls_back(client, test_engine):
    """An unrecognized preset doesn't 500; falls back to default."""
    _seed_movement_population()
    r = client.get("/movers?preset=__nonsense__")
    assert r.status_code == 200
    # Default preset is rising_star — page should still render
    assert "rising star" in r.text  # in the description prose


def test_movers_page_invalid_days_falls_back(client, test_engine):
    """Bad days param falls back to default 7."""
    _seed_movement_population()
    r = client.get("/movers?days=999")
    assert r.status_code == 200
    # Default is 7 days, mentioned in the prose
    assert "7 day" in r.text


def test_movers_page_empty_state(client, test_engine):
    """No data yet → friendly empty-state message."""
    r = client.get("/movers")
    assert r.status_code == 200
    assert "No rank movement" in r.text


def test_movers_nav_link_on_dashboard(client, test_engine):
    """The /movers link appears in the nav."""
    r = client.get("/")
    assert r.status_code == 200
    assert "/movers" in r.text
    assert "Movers" in r.text


def test_movers_page_supports_hidden_gem(client, test_engine):
    """Switching preset query param renders for hidden_gem too."""
    _seed_movement_population()
    # Re-add a hidden_gem score so the page has something to show
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name="Climber").one()
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=10)
        session.add(Score(
            maker_id=m.id, preset="hidden_gem", value=1.0, rank=10,
            computed_at=old,
        ))
        session.add(Score(
            maker_id=m.id, preset="hidden_gem", value=2.0, rank=2,
            computed_at=now - timedelta(minutes=1),
        ))
    r = client.get("/movers?preset=hidden_gem")
    assert r.status_code == 200
    assert "hidden gem" in r.text
