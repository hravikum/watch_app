"""Tests for the /comparison page.

Covers:
  1. fetch_all_makers_comparison helper — combining two preset rankings
  2. Handling of makers with scores in only one preset
  3. Divergence computation
  4. Route rendering with each sort key
  5. Empty database case
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.scoring.queries import fetch_all_makers_comparison
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score, Source as SourceRow


def _seed_two_preset_population():
    """Build a population where rising_star and hidden_gem produce different
    rankings, with at least one maker scored in only one preset.

    Five makers:
      - A: rank 1 in rising_star, rank 5 in hidden_gem (high divergence, 4)
      - B: rank 2 in rising_star, rank 1 in hidden_gem (divergence 1)
      - C: rank 3 in rising_star, rank 3 in hidden_gem (divergence 0)
      - D: rank 4 in rising_star, ABSENT from hidden_gem (filtered out)
      - E: ABSENT from rising_star, rank 2 in hidden_gem
    """
    with session_scope() as session:
        src = SourceRow(name="Test", category="media", prestige_weight=1.0)
        session.add(src)
        session.flush()

        names = ["Maker A", "Maker B", "Maker C", "Maker D", "Maker E"]
        countries = ["Switzerland", "Germany", "Japan", "France", "UK"]
        makers = []
        for n, c in zip(names, countries):
            m = Maker(canonical_name=n, country=c, is_independent=True)
            session.add(m)
            makers.append(m)
        session.flush()

        # Add a few mentions so mention_count is non-zero for some
        for i, m in enumerate(makers):
            for j in range(i + 1):  # 1, 2, 3, 4, 5 mentions respectively
                session.add(Mention(
                    maker_id=m.id, source_id=src.id,
                    url=f"http://example.com/{m.canonical_name}/{j}",
                    title=f"about {m.canonical_name} #{j}",
                    discovered_at=datetime.now(timezone.utc),
                ))

        # Scores under rising_star
        ts = datetime.now(timezone.utc)
        rs_data = [(makers[0], 5.0, 1), (makers[1], 4.0, 2),
                   (makers[2], 3.0, 3), (makers[3], 2.0, 4)]
        for m, val, rk in rs_data:
            session.add(Score(
                maker_id=m.id, preset="rising_star",
                value=val, rank=rk, computed_at=ts,
            ))

        # Scores under hidden_gem — note Maker D absent, Maker E present
        hg_data = [(makers[1], 5.0, 1), (makers[4], 4.0, 2),
                   (makers[2], 3.0, 3), (makers[0], 1.0, 5)]
        for m, val, rk in hg_data:
            session.add(Score(
                maker_id=m.id, preset="hidden_gem",
                value=val, rank=rk, computed_at=ts,
            ))


# ---------- Helper tests ----------


def test_comparison_returns_union_of_makers(test_engine):
    """Should include every maker scored in either preset, not just both."""
    _seed_two_preset_population()
    with session_scope() as session:
        rows = fetch_all_makers_comparison(session, "rising_star", "hidden_gem")
    names = {r["canonical_name"] for r in rows}
    # All 5 makers should appear: 4 in RS, 4 in HG, union = 5
    assert names == {"Maker A", "Maker B", "Maker C", "Maker D", "Maker E"}


def test_comparison_handles_missing_preset(test_engine):
    """A maker with no score under one preset should have None for that
    preset's rank/score, but still appear."""
    _seed_two_preset_population()
    with session_scope() as session:
        rows = fetch_all_makers_comparison(session, "rising_star", "hidden_gem")
    by_name = {r["canonical_name"]: r for r in rows}

    # Maker D has only rising_star
    d = by_name["Maker D"]
    assert d["rank_a"] == 4
    assert d["score_a"] == 2.0
    assert d["rank_b"] is None
    assert d["score_b"] is None
    assert d["divergence"] is None

    # Maker E has only hidden_gem
    e = by_name["Maker E"]
    assert e["rank_a"] is None
    assert e["score_a"] is None
    assert e["rank_b"] == 2
    assert e["score_b"] == 4.0
    assert e["divergence"] is None


def test_comparison_computes_divergence(test_engine):
    """Divergence is |rank_a - rank_b| when both ranks are present."""
    _seed_two_preset_population()
    with session_scope() as session:
        rows = fetch_all_makers_comparison(session, "rising_star", "hidden_gem")
    by_name = {r["canonical_name"]: r for r in rows}

    # A: rank 1 in RS, rank 5 in HG → divergence 4
    assert by_name["Maker A"]["divergence"] == 4
    # B: rank 2 in RS, rank 1 in HG → divergence 1
    assert by_name["Maker B"]["divergence"] == 1
    # C: rank 3 in both → divergence 0
    assert by_name["Maker C"]["divergence"] == 0


def test_comparison_includes_country_and_mentions(test_engine):
    """The view passes through country and mention_count for display."""
    _seed_two_preset_population()
    with session_scope() as session:
        rows = fetch_all_makers_comparison(session, "rising_star", "hidden_gem")
    by_name = {r["canonical_name"]: r for r in rows}

    assert by_name["Maker A"]["country"] == "Switzerland"
    assert by_name["Maker A"]["mention_count"] == 1
    assert by_name["Maker E"]["mention_count"] == 5


def test_comparison_handles_empty_db(test_engine):
    """No scores, no makers — should return empty list, not error."""
    with session_scope() as session:
        rows = fetch_all_makers_comparison(session, "rising_star", "hidden_gem")
    assert rows == []


# ---------- Route tests ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def test_comparison_page_renders_default_sort(client, test_engine):
    """Page loads with default sort (rank_a ascending)."""
    _seed_two_preset_population()
    r = client.get("/comparison")
    assert r.status_code == 200
    assert "Maker Comparison" in r.text
    # All five makers should be in the rendered HTML
    for n in ["Maker A", "Maker B", "Maker C", "Maker D", "Maker E"]:
        assert n in r.text
    # Default sort: Maker A (rank 1 in RS) should appear before Maker D (rank 4)
    assert _find_maker_in_table(r.text, "Maker A") < _find_maker_in_table(r.text, "Maker D")


def _find_maker_in_table(text: str, name: str) -> int:
    """Position of a maker's name in its table-row link, not in any
    other prose on the page (the title 'Maker Comparison' contains
    'Maker C' as a substring, which previously broke find-based
    ordering tests)."""
    needle = f">{name}</a>"
    return text.find(needle)


def test_comparison_page_sorts_by_divergence(client, test_engine):
    """Sorting by divergence puts highest-divergence makers first."""
    _seed_two_preset_population()
    r = client.get("/comparison?sort=divergence")
    assert r.status_code == 200
    # Maker A has divergence 4 (highest with both ranks); Maker C has 0
    assert _find_maker_in_table(r.text, "Maker A") < _find_maker_in_table(r.text, "Maker C")
    # Makers with no divergence (D, E) should sort last
    assert _find_maker_in_table(r.text, "Maker A") < _find_maker_in_table(r.text, "Maker D")
    assert _find_maker_in_table(r.text, "Maker A") < _find_maker_in_table(r.text, "Maker E")


def test_comparison_page_sorts_by_mentions(client, test_engine):
    """Sorting by mentions descending: most mentions first."""
    _seed_two_preset_population()
    r = client.get("/comparison?sort=mentions")
    # Maker E has 5 mentions, Maker A has 1
    assert _find_maker_in_table(r.text, "Maker E") < _find_maker_in_table(r.text, "Maker A")


def test_comparison_page_unknown_sort_falls_back(client, test_engine):
    """An unrecognized sort key shouldn't 500; it falls back to default."""
    _seed_two_preset_population()
    r = client.get("/comparison?sort=__nonsense__")
    assert r.status_code == 200
    # Should still render normally with rank_a default
    assert "Maker A" in r.text


def test_comparison_page_empty(client, test_engine):
    """With no scored makers, page still loads with a friendly message."""
    r = client.get("/comparison")
    assert r.status_code == 200
    assert "No scores have been computed yet" in r.text


def test_comparison_page_links_to_methodology(client, test_engine):
    """The footer note about preset gates should link to /methodology."""
    _seed_two_preset_population()
    r = client.get("/comparison")
    assert "/methodology" in r.text


def test_comparison_nav_link_on_dashboard(client, test_engine):
    """The /comparison link should appear in the nav on every page."""
    r = client.get("/")
    assert r.status_code == 200
    assert "/comparison" in r.text
    assert "Comparison" in r.text
