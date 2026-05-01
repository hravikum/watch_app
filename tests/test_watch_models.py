"""Tests for the WatchModel persistence layer.

Covers:
  1. _extract_model_name_from_title — parses the model name from the GPHG
     mention title format we produce
  2. _fetch_gphg_models — orchestrator step that reads GPHG mentions and
     populates WatchModel rows; mocks the HTTP layer
  3. The maker detail page renders the GPHG-nominated Watches section
     when the maker has WatchModel rows
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.pipeline.orchestrator import (
    _extract_model_name_from_title,
    _fetch_gphg_models,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import (
    Maker, Mention, Source as SourceRow, WatchModel,
)


FIXTURES = Path(__file__).parent / "fixtures"


# ---------- Title parser ----------


@pytest.mark.parametrize("title,expected", [
    ("GPHG 2025 nominee — Parmigiani Fleurier: Tonda PF Micro-Rotor",
     "Tonda PF Micro-Rotor"),
    ("GPHG 2024 nominee — F.P. Journe: Chronomètre à Résonance",
     "Chronomètre à Résonance"),
    ("GPHG 2023 nominee — Simon Brette: Chronomètre Artisans",
     "Chronomètre Artisans"),
    # Edge cases
    ("Random title without proper format", None),
    ("", None),
    (None, None),
    ("GPHG 2025 nominee — No colon here", None),
])
def test_extract_model_name_from_title(title, expected):
    assert _extract_model_name_from_title(title) == expected


# ---------- Orchestrator integration ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def _seed_gphg_mention(maker_name: str, model_name: str, year: int = 2025):
    """Create a maker, GPHG source, and a mention as the orchestrator
    would have. Returns (maker_id, mention_url).

    The source row is reused if one with the same name already exists —
    multiple makers can be nominated in the same edition."""
    with session_scope() as session:
        src = (
            session.query(SourceRow)
            .filter(SourceRow.name == f"GPHG {year}")
            .one_or_none()
        )
        if src is None:
            src = SourceRow(name=f"GPHG {year}", category="awards", prestige_weight=1.2)
            session.add(src)
            session.flush()

        maker = Maker(canonical_name=maker_name, is_independent=True)
        session.add(maker)
        session.flush()

        url = f"https://www.gphg.org/en/watches/test-{model_name.lower().replace(' ', '-')}"
        session.add(Mention(
            maker_id=maker.id,
            source_id=src.id,
            url=url,
            title=f"GPHG {year} nominee — {maker_name}: {model_name}",
            excerpt="Test mention",
            published_at=datetime(year, 10, 15, tzinfo=timezone.utc),
        ))
        return maker.id, url


def _make_mock_response(html: str):
    """Mimic the shape of what watch_app.http.get returns."""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    resp.from_cache = False
    return resp


def test_fetch_gphg_models_creates_rows(test_engine):
    """Given a GPHG mention with no corresponding WatchModel, the
    orchestrator step should fetch its detail page, parse it, and
    create one WatchModel row."""
    maker_id, url = _seed_gphg_mention("Test Maker", "Test Watch", year=2025)
    fixture_html = (FIXTURES / "gphg_detail_full.html").read_text()

    with patch("watch_app.http.get", return_value=_make_mock_response(fixture_html)):
        with session_scope() as session:
            count = _fetch_gphg_models(session)

    assert count == 1

    with session_scope() as session:
        models = session.query(WatchModel).all()
        assert len(models) == 1
        m = models[0]
        assert m.maker_id == maker_id
        assert m.name == "Test Watch"
        assert m.gphg_year == 2025
        assert m.gphg_url == url
        # Specs from the fixture
        assert m.case_diameter_mm == 40.0
        assert m.water_resistance_m == 100
        assert m.movement_type == "automatic"
        assert m.power_reserve_hours == 48
        assert m.price_chf == 85000


def test_fetch_gphg_models_skips_existing(test_engine):
    """A second invocation should NOT refetch URLs that already have a
    WatchModel — those detail pages don't change once the year ends."""
    _seed_gphg_mention("Maker A", "Watch 1")
    fixture_html = (FIXTURES / "gphg_detail_full.html").read_text()

    with patch("watch_app.http.get", return_value=_make_mock_response(fixture_html)) as mock_get:
        with session_scope() as session:
            _fetch_gphg_models(session)
            assert mock_get.call_count == 1

        # Second invocation: nothing new to fetch
        with session_scope() as session:
            count = _fetch_gphg_models(session)
            assert count == 0
            assert mock_get.call_count == 1  # No additional fetches


def test_fetch_gphg_models_handles_fetch_failure(test_engine):
    """If a single detail page fetch fails, the function should log it,
    skip that URL, and continue. The next scan can retry."""
    _seed_gphg_mention("Maker A", "Watch 1")

    def raise_on_fetch(*args, **kwargs):
        raise RuntimeError("simulated network error")

    with patch("watch_app.http.get", side_effect=raise_on_fetch):
        with session_scope() as session:
            count = _fetch_gphg_models(session)

    assert count == 0
    with session_scope() as session:
        assert session.query(WatchModel).count() == 0


def test_fetch_gphg_models_continues_past_one_bad_url(test_engine):
    """One bad fetch shouldn't prevent later ones from succeeding."""
    _seed_gphg_mention("Maker A", "Watch A")
    _seed_gphg_mention("Maker B", "Watch B")
    fixture_html = (FIXTURES / "gphg_detail_full.html").read_text()

    call_count = {"n": 0}
    def fetch_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first one fails")
        return _make_mock_response(fixture_html)

    with patch("watch_app.http.get", side_effect=fetch_side_effect):
        with session_scope() as session:
            count = _fetch_gphg_models(session)

    assert count == 1  # one succeeded, one failed
    with session_scope() as session:
        assert session.query(WatchModel).count() == 1


def test_fetch_gphg_models_skips_non_200(test_engine):
    """A non-200 response should be skipped, not parsed."""
    _seed_gphg_mention("Maker A", "Watch 1")

    bad = MagicMock()
    bad.status_code = 404
    bad.text = "<html>404</html>"

    with patch("watch_app.http.get", return_value=bad):
        with session_scope() as session:
            count = _fetch_gphg_models(session)

    assert count == 0


def test_fetch_gphg_models_no_awards_sources(test_engine):
    """An empty DB or one with no awards sources should not crash."""
    with session_scope() as session:
        count = _fetch_gphg_models(session)
    assert count == 0


# ---------- Maker detail page rendering ----------


def test_detail_page_shows_models_when_present(client, test_engine):
    """A maker with WatchModel rows should see them rendered on their
    detail page."""
    maker_id, url = _seed_gphg_mention("Test Brand", "Test Tonda PF", year=2025)

    with session_scope() as session:
        session.add(WatchModel(
            maker_id=maker_id,
            name="Test Tonda PF",
            slug="test-tonda-pf",
            gphg_year=2025,
            gphg_url=url,
            case_material="Platinum",
            case_diameter_mm=40.0,
            movement_type="automatic",
            power_reserve_hours=48,
            water_resistance_m=100,
            price_chf=85000,
            limited_edition_count=25,
        ))

    r = client.get(f"/makers/{maker_id}")
    assert r.status_code == 200

    assert "GPHG-nominated Watches" in r.text
    assert "Test Tonda PF" in r.text
    assert "GPHG 2025" in r.text
    assert "40.0 mm" in r.text
    assert "Platinum" in r.text
    assert "Automatic" in r.text  # title-cased in template
    assert "48 h" in r.text
    assert "100 m" in r.text
    assert "85,000 CHF" in r.text
    assert "25 pieces" in r.text


def test_detail_page_omits_section_when_no_models(client, test_engine):
    """A maker with no WatchModel rows should not have the section render
    (no empty-state placeholder for this section — keep the page clean)."""
    with session_scope() as session:
        maker = Maker(canonical_name="Bare Maker", is_independent=True)
        session.add(maker)
        session.flush()
        maker_id = maker.id

    r = client.get(f"/makers/{maker_id}")
    assert r.status_code == 200
    assert "GPHG-nominated Watches" not in r.text


def test_detail_page_handles_partial_specs(client, test_engine):
    """A WatchModel with most fields NULL should render only the fields
    that have values — no empty rows."""
    maker_id, url = _seed_gphg_mention("Sparse Brand", "Sparse Watch")

    with session_scope() as session:
        session.add(WatchModel(
            maker_id=maker_id,
            name="Sparse Watch",
            slug="sparse-watch",
            gphg_year=2024,
            gphg_url=url,
            case_diameter_mm=38.0,
            # everything else NULL
        ))

    r = client.get(f"/makers/{maker_id}")
    assert r.status_code == 200
    assert "Sparse Watch" in r.text
    assert "38.0 mm" in r.text
    # Fields that aren't set shouldn't appear
    assert "Power reserve" not in r.text
    assert "Water resistance" not in r.text
    assert "Limited edition" not in r.text
