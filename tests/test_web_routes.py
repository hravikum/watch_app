"""Smoke tests for the FastAPI routes.

Covers the dashboard, preset fragment, and maker detail page. We use
TestClient with a real SQLAlchemy session scoped to the test DB.

These are integration-lite: we mount the real router, seed a tiny DB,
and assert the HTML responses contain the content we expect. No browser.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.scoring.ranker import rank_all_makers
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Source as SourceRow


@pytest.fixture
def client(test_engine):
    """A FastAPI TestClient wired to the test engine."""
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def _seed_minimal_data():
    """Write two makers and a handful of mentions so pages have content."""
    with session_scope() as session:
        src = SourceRow(
            name="TestPub", category="media", prestige_weight=0.8,
        )
        session.add(src)
        session.flush()

        indie = Maker(
            canonical_name="Test Indie",
            aliases=["TI"],
            country="Switzerland",
            founded_year=2015,
            is_independent=True,
        )
        big = Maker(
            canonical_name="Test Conglomerate",
            aliases=[],
            country="Switzerland",
            is_independent=False,
        )
        session.add_all([indie, big])
        session.flush()

        session.add_all([
            Mention(
                maker_id=indie.id,
                source_id=src.id,
                url="https://testpub.example/a",
                title="Hands-on with Test Indie",
                excerpt="A thoughtful dive into the workshop.",
                published_at=datetime(2025, 10, 1, tzinfo=timezone.utc),
            ),
            Mention(
                maker_id=big.id,
                source_id=src.id,
                url="https://testpub.example/b",
                title="Test Conglomerate's latest",
                published_at=datetime(2025, 9, 1, tzinfo=timezone.utc),
            ),
        ])

        rank_all_makers(session, "rising_star", normalize=False)
        rank_all_makers(session, "hidden_gem", normalize=False)

        return indie.id, big.id


def test_dashboard_renders(client):
    """GET / should render a dashboard with the expected structure."""
    _seed_minimal_data()
    r = client.get("/")
    assert r.status_code == 200
    assert "Top Makers" in r.text
    assert "Test Indie" in r.text


def test_preset_fragment_swappable(client):
    """GET /makers/top?preset=hidden_gem returns the partial (not full page)."""
    _seed_minimal_data()
    r = client.get("/makers/top?preset=hidden_gem")
    assert r.status_code == 200
    # The fragment should NOT contain the full <header> / sticky nav.
    assert "sticky top-0" not in r.text
    # But it should contain the makers table markup.
    assert "Mentions" in r.text or "No makers" in r.text


def test_maker_detail_page_renders(client):
    """GET /makers/{id} produces a detail page with key content."""
    indie_id, _ = _seed_minimal_data()
    r = client.get(f"/makers/{indie_id}")
    assert r.status_code == 200
    # Header
    assert "Test Indie" in r.text
    # Metadata
    assert "Switzerland" in r.text
    assert "2015" in r.text
    assert "Also known as" in r.text
    assert "TI" in r.text
    # Score panels — should show both presets
    assert "Rising Star" in r.text
    assert "Hidden Gem" in r.text
    # Signal breakdown — now a per-maker contribution table (labels, not raw keys)
    assert "Signal Breakdown" in r.text
    assert "Mention count" in r.text
    assert "Contribution" in r.text
    # Mention list
    assert "Hands-on with Test Indie" in r.text
    assert "TestPub" in r.text
    # Back link
    assert "Back to dashboard" in r.text


def test_maker_detail_shows_conglomerate_badge(client):
    """Non-independent makers get a visible 'Conglomerate' badge."""
    _, big_id = _seed_minimal_data()
    r = client.get(f"/makers/{big_id}")
    assert r.status_code == 200
    assert "Conglomerate" in r.text


def test_maker_detail_404_for_missing(client):
    """Requesting a nonexistent maker returns 404."""
    r = client.get("/makers/99999")
    assert r.status_code == 404


def test_maker_rows_link_to_detail_page(client):
    """Dashboard table rows should include hrefs pointing at detail pages."""
    indie_id, _ = _seed_minimal_data()
    r = client.get("/")
    assert r.status_code == 200
    # Each row should have a link into /makers/<id>.
    assert f'href="/makers/{indie_id}"' in r.text


def _seed_maker_with_website(website: str | None):
    """Create a single maker with a specific website value, return id."""
    with session_scope() as session:
        m = Maker(
            canonical_name="Web Test Maker",
            country="Switzerland",
            founded_year=2010,
            is_independent=True,
            website=website,
        )
        session.add(m)
        session.flush()
        rank_all_makers(session, "rising_star", normalize=False)
        rank_all_makers(session, "hidden_gem", normalize=False)
        return m.id


def test_maker_detail_renders_website_link(client):
    """When a maker has a website, the detail page renders a clickable
    link with target=_blank and rel=noopener for security."""
    maker_id = _seed_maker_with_website("https://example-watchmaker.com")
    r = client.get(f"/makers/{maker_id}")
    assert r.status_code == 200
    assert 'href="https://example-watchmaker.com"' in r.text
    # Opens in new tab so user doesn't lose the maker page
    assert 'target="_blank"' in r.text
    # Standard security mitigation for external links
    assert 'rel="noopener"' in r.text


def test_maker_detail_omits_website_when_none(client):
    """When a maker has no website, the page renders without crashing
    and without an empty website element."""
    maker_id = _seed_maker_with_website(None)
    r = client.get(f"/makers/{maker_id}")
    assert r.status_code == 200
    # No website link should be present in the metadata row
    assert "Website ↗" not in r.text
