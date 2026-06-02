"""Tests for the taste explorer (Layer 2a, chunk 3J).

Covers the faceted match logic (OR within a dimension, AND across), the static
facet counts, the tagged-universe gate, and the page + results routes.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import (
    _facet_counts,
    _maker_has_taste,
    _maker_matches_filters,
    router as makers_router,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


# ---------- Pure helpers ----------


def _m(**kw) -> Maker:
    return Maker(canonical_name=kw.pop("name", "X"), **kw)


def test_has_taste():
    assert _maker_has_taste(_m(aesthetic_tags=["tool"]))
    assert _maker_has_taste(_m(maker_type="solo-watchmaker"))
    assert not _maker_has_taste(_m())


def test_match_multi_value_overlap():
    m = _m(aesthetic_tags=["tool", "modern"])
    assert _maker_matches_filters(m, {"aesthetic_tags": {"tool"}})
    assert not _maker_matches_filters(m, {"aesthetic_tags": {"dressy"}})
    # OR within a dimension: any overlap matches.
    assert _maker_matches_filters(m, {"aesthetic_tags": {"dressy", "modern"}})


def test_match_single_value():
    m = _m(price_band="mid")
    assert _maker_matches_filters(m, {"price_band": {"mid"}})
    assert not _maker_matches_filters(m, {"price_band": {"high"}})
    assert _maker_matches_filters(m, {"price_band": {"mid", "high"}})  # OR within


def test_match_and_across_dimensions():
    m = _m(aesthetic_tags=["tool"], price_band="mid")
    assert _maker_matches_filters(m, {"aesthetic_tags": {"tool"}, "price_band": {"mid"}})
    # One dimension mismatch fails the whole match (AND across).
    assert not _maker_matches_filters(
        m, {"aesthetic_tags": {"tool"}, "price_band": {"high"}}
    )


def test_empty_selection_is_ignored():
    m = _m(aesthetic_tags=["tool"])
    assert _maker_matches_filters(m, {"aesthetic_tags": set(), "price_band": set()})


def test_facet_counts():
    makers = [
        _m(aesthetic_tags=["tool", "modern"], price_band="mid"),
        _m(aesthetic_tags=["tool"], price_band="high"),
        _m(aesthetic_tags=["dressy"], price_band="mid"),
    ]
    counts = _facet_counts(makers)
    assert counts["aesthetic_tags"]["tool"] == 2
    assert counts["aesthetic_tags"]["dressy"] == 1
    assert counts["price_band"]["mid"] == 2
    assert counts["price_band"]["high"] == 1


# ---------- Routes ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def _seed(name, **taste_kw):
    with session_scope() as session:
        session.add(Maker(canonical_name=name, **taste_kw))


def test_explore_page_renders_with_counts(client):
    _seed("Tooly", aesthetic_tags=["tool"])
    _seed("Dressy One", aesthetic_tags=["dressy"])
    r = client.get("/explore")
    assert r.status_code == 200
    assert "Explore by taste" in r.text
    # Both tagged makers show on the unfiltered page.
    assert "Tooly" in r.text and "Dressy One" in r.text


def test_results_or_within_dimension(client):
    _seed("Tooly", aesthetic_tags=["tool"])
    _seed("Dressy One", aesthetic_tags=["dressy"])
    # Single value → only the matching maker.
    r = client.get("/explore/results", params={"aesthetic_tags": "tool"})
    assert "Tooly" in r.text and "Dressy One" not in r.text
    # Two values → OR within → both.
    r2 = client.get(
        "/explore/results", params={"aesthetic_tags": ["tool", "dressy"]}
    )
    assert "Tooly" in r2.text and "Dressy One" in r2.text


def test_results_and_across_dimensions(client):
    _seed("Mid Tool", aesthetic_tags=["tool"], price_band="mid")
    _seed("High Tool", aesthetic_tags=["tool"], price_band="high")
    r = client.get(
        "/explore/results",
        params={"aesthetic_tags": "tool", "price_band": "mid"},
    )
    assert "Mid Tool" in r.text and "High Tool" not in r.text


def test_untagged_maker_absent(client):
    _seed("Tagged", maker_type="solo-watchmaker")
    _seed("Untagged")  # no taste at all
    r = client.get("/explore")
    assert "Tagged" in r.text and "Untagged" not in r.text


def test_no_match_empty_state(client):
    _seed("Tooly", aesthetic_tags=["tool"])
    r = client.get("/explore/results", params={"aesthetic_tags": "dressy"})
    assert "No tagged makers match" in r.text



# ---------- Window filter (mentioned vs discovered) ----------


def test_window_discovered_filters_by_first_seen(client):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(Maker(canonical_name="FreshDisc", aesthetic_tags=["tool"],
                          first_seen_at=now - timedelta(days=2)))
        session.add(Maker(canonical_name="StaleDisc", aesthetic_tags=["tool"],
                          first_seen_at=now - timedelta(days=60)))

    # All-time shows both.
    r = client.get("/explore/results")
    assert "FreshDisc" in r.text and "StaleDisc" in r.text

    # 7-day discovered window keeps only the recent one.
    r = client.get("/explore/results", params={"window": "7", "window_by": "discovered"})
    assert "FreshDisc" in r.text and "StaleDisc" not in r.text


def test_window_activity_filters_by_recent_mention(client):
    from datetime import datetime, timedelta, timezone

    from watch_app.storage.models import Mention, Source

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        src = Source(name="WinSrc", category="media", prestige_weight=1.0)
        recent = Maker(canonical_name="RecentlyTalked", aesthetic_tags=["tool"],
                       first_seen_at=now - timedelta(days=90))
        quiet = Maker(canonical_name="WentQuiet", aesthetic_tags=["tool"],
                      first_seen_at=now - timedelta(days=90))
        session.add_all([src, recent, quiet])
        session.flush()
        session.add(Mention(maker_id=recent.id, source_id=src.id, url="http://x/a",
                            title="a", published_at=now - timedelta(days=3)))
        session.add(Mention(maker_id=quiet.id, source_id=src.id, url="http://x/b",
                            title="b", published_at=now - timedelta(days=120)))

    # 7-day activity window: only the recently-mentioned one.
    r = client.get("/explore/results", params={"window": "7", "window_by": "activity"})
    assert "RecentlyTalked" in r.text and "WentQuiet" not in r.text

    # All-time: both (both are tagged).
    r = client.get("/explore/results")
    assert "RecentlyTalked" in r.text and "WentQuiet" in r.text


def test_window_all_time_is_default(client):
    _seed("AlwaysThere", aesthetic_tags=["tool"])
    r = client.get("/explore/results", params={"window": "all"})
    assert "AlwaysThere" in r.text
