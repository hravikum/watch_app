"""Tests for the per-source detail page (chunk 3M)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.scoring.methodology import build_source_detail
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Source


@pytest.fixture
def data(test_engine):
    """Two sources; Alpha first-surfaces M1 & M2, Beta first-surfaces M3."""
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(hours=3)
    with session_scope() as s:
        alpha = Source(name="Alpha", category="media", prestige_weight=1.0, active=True)
        beta = Source(name="Beta", category="community", prestige_weight=0.5, active=False)
        s.add_all([alpha, beta])
        s.flush()
        m1 = Maker(canonical_name="M1 Maker")
        m2 = Maker(canonical_name="M2 Maker")
        m3 = Maker(canonical_name="M3 Maker")
        s.add_all([m1, m2, m3])
        s.flush()

        def men(src, mk, dt, title, url):
            s.add(Mention(source_id=src, maker_id=mk, discovered_at=dt, title=title, url=url))

        # M1: Alpha first (t0), Beta later → Alpha surfaced M1.
        men(alpha.id, m1.id, t0, "Alpha on M1", "https://x/1")
        men(beta.id, m1.id, t0 + timedelta(hours=1), "Beta on M1", "https://x/2")
        # M2: Alpha twice → Alpha surfaced; Alpha's most-covered.
        men(alpha.id, m2.id, t0 + timedelta(minutes=10), "Alpha on M2 a", "https://x/3")
        men(alpha.id, m2.id, t0 + timedelta(minutes=20), "Alpha on M2 b", "https://x/4")
        # M3: Beta only → Beta surfaced.
        men(beta.id, m3.id, t0, "Beta on M3", "https://x/5")
        return {"alpha": alpha.id, "beta": beta.id}


def test_detail_stats(data):
    with session_scope() as s:
        d = build_source_detail(s, data["alpha"])
    assert d.source_name == "Alpha"
    assert d.total_mentions == 3       # m1 once + m2 twice
    assert d.distinct_makers == 2      # m1, m2
    assert d.mentions_recent == 3      # all within the window
    assert d.prestige_weight == 1.0
    assert d.active is True


def test_first_surfaced_credit(data):
    with session_scope() as s:
        alpha = build_source_detail(s, data["alpha"])
        beta = build_source_detail(s, data["beta"])
    assert [m["name"] for m in alpha.first_surfaced] == ["M1 Maker", "M2 Maker"]
    assert alpha.first_surfaced_count == 2
    # Beta only first-surfaced M3 (it was second on M1).
    assert [m["name"] for m in beta.first_surfaced] == ["M3 Maker"]


def test_top_makers_ordered(data):
    with session_scope() as s:
        d = build_source_detail(s, data["alpha"])
    assert d.top_makers[0]["name"] == "M2 Maker"
    assert d.top_makers[0]["mention_count"] == 2
    assert {m["name"] for m in d.top_makers} == {"M1 Maker", "M2 Maker"}


def test_recent_mentions(data):
    with session_scope() as s:
        d = build_source_detail(s, data["alpha"])
    assert len(d.recent_mentions) == 3
    # Newest first.
    assert d.recent_mentions[0]["when"] >= d.recent_mentions[-1]["when"]
    assert all(m["maker_name"] for m in d.recent_mentions)


def test_missing_source_returns_none(test_engine):
    with session_scope() as s:
        assert build_source_detail(s, 999999) is None


# ---- route ----


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def test_source_page_renders(client, data):
    r = client.get(f"/sources/{data['alpha']}")
    assert r.status_code == 200
    assert "Alpha" in r.text
    assert "M2 Maker" in r.text            # most-covered
    assert "Most-covered makers" in r.text


def test_inactive_badge_shown(client, data):
    r = client.get(f"/sources/{data['beta']}")
    assert r.status_code == 200
    assert "inactive" in r.text

def test_source_page_404(client):
    assert client.get("/sources/999999").status_code == 404
