"""Tests for the discoveries view — auto-discovered makers visibility.

Verifies:
  1. Seed name set loads from YAML correctly (canonical names + aliases)
  2. Definition A filter: makers in seed are excluded; auto-discovered included
  3. Mention counts and source names are accurate (no N+1 misses)
  4. DiscoveredMaker.missing_fields reports correctly
  5. DiscoveredMaker.field_was_enriched reports correctly
  6. /discoveries route renders without error
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.scoring.discovery import (
    DiscoveredMaker,
    _load_seed_names,
    fetch_discovered_makers,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Source


@pytest.fixture
def client(test_engine):
    """Test HTTP client wired up with the makers router."""
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


# ---------- Seed name loading ----------


def test_load_seed_names_includes_canonical():
    """Seed canonical names should appear in the loaded set."""
    names = _load_seed_names()
    # Use makers we know are in the real seed YAML
    assert "Simon Brette" in names
    assert "Voutilainen" in names
    assert "Philippe Dufour" in names


def test_load_seed_names_includes_aliases():
    """Seed aliases should also appear (so a maker known by alias isn't
    flagged as auto-discovered)."""
    names = _load_seed_names()
    # Kurono Tokyo has aliases including "Kurono" (added in 2R-hotfix)
    assert "Kurono Tokyo" in names
    assert "Kurono" in names


# ---------- Definition A filter ----------


def test_seed_maker_excluded_from_discoveries(test_engine):
    """A maker whose canonical_name is in the seed YAML should NOT
    appear in the discovery view."""
    with session_scope() as session:
        # Simon Brette IS in the seed YAML
        session.add(Maker(canonical_name="Simon Brette", is_independent=True))

    with session_scope() as session:
        discovered = fetch_discovered_makers(session)
    names = [d.canonical_name for d in discovered]
    assert "Simon Brette" not in names


def test_non_seed_maker_included_in_discoveries(test_engine):
    """A maker with a name not in the seed YAML SHOULD appear."""
    with session_scope() as session:
        session.add(Maker(canonical_name="Definitely Not A Real Maker", is_independent=True))

    with session_scope() as session:
        discovered = fetch_discovered_makers(session)
    names = [d.canonical_name for d in discovered]
    assert "Definitely Not A Real Maker" in names


def test_maker_with_seed_alias_excluded(test_engine):
    """If a Maker's canonical_name matches a seed alias (not the seed's
    canonical name), it should still be excluded — the seed knows about
    it under a different name."""
    with session_scope() as session:
        # "Kurono" is an alias for Kurono Tokyo in the seed
        session.add(Maker(canonical_name="Kurono", is_independent=True))

    with session_scope() as session:
        discovered = fetch_discovered_makers(session)
    names = [d.canonical_name for d in discovered]
    assert "Kurono" not in names


# ---------- Sorting and content ----------


def test_discoveries_sorted_by_first_seen_desc(test_engine):
    """Most recent discoveries appear first."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(Maker(
            canonical_name="Old Discovery",
            is_independent=True,
            first_seen_at=now - timedelta(days=30),
        ))
        session.add(Maker(
            canonical_name="New Discovery",
            is_independent=True,
            first_seen_at=now - timedelta(days=1),
        ))
        session.add(Maker(
            canonical_name="Middle Discovery",
            is_independent=True,
            first_seen_at=now - timedelta(days=15),
        ))

    with session_scope() as session:
        discovered = fetch_discovered_makers(session)
    names = [d.canonical_name for d in discovered if d.canonical_name.endswith("Discovery")]
    assert names == ["New Discovery", "Middle Discovery", "Old Discovery"]


def test_mention_counts_attached(test_engine):
    """DiscoveredMaker.mention_count should reflect actual mentions."""
    with session_scope() as session:
        m = Maker(canonical_name="Maker With Mentions", is_independent=True)
        s = Source(name="test_source", category="media", prestige_weight=0.5)
        session.add_all([m, s])
        session.flush()
        for i in range(3):
            session.add(Mention(
                maker_id=m.id,
                source_id=s.id,
                title=f"Test mention {i}",
                url=f"https://example.com/{i}",
                published_at=datetime.now(timezone.utc),
            ))

    with session_scope() as session:
        discovered = fetch_discovered_makers(session)
    found = next((d for d in discovered if d.canonical_name == "Maker With Mentions"), None)
    assert found is not None
    assert found.mention_count == 3


def test_source_names_attached_and_deduplicated(test_engine):
    """source_names should list distinct sources, alphabetically sorted."""
    with session_scope() as session:
        m = Maker(canonical_name="Multi-source Maker", is_independent=True)
        s1 = Source(name="zebra", category="media", prestige_weight=0.5)
        s2 = Source(name="alpha", category="media", prestige_weight=0.5)
        session.add_all([m, s1, s2])
        session.flush()
        # Two mentions from zebra, one from alpha — sources should appear once each
        session.add(Mention(maker_id=m.id, source_id=s1.id, title="t1",
                            url="https://z.com/1", published_at=datetime.now(timezone.utc)))
        session.add(Mention(maker_id=m.id, source_id=s1.id, title="t2",
                            url="https://z.com/2", published_at=datetime.now(timezone.utc)))
        session.add(Mention(maker_id=m.id, source_id=s2.id, title="t3",
                            url="https://a.com/1", published_at=datetime.now(timezone.utc)))

    with session_scope() as session:
        discovered = fetch_discovered_makers(session)
    found = next((d for d in discovered if d.canonical_name == "Multi-source Maker"), None)
    assert found is not None
    assert found.source_names == ["alpha", "zebra"]  # sorted, deduplicated


# ---------- Dataclass properties ----------


def test_missing_fields_all_present():
    """All key fields populated → no missing fields."""
    m = DiscoveredMaker(
        id=1, canonical_name="X", country="Japan", founded_year=2020,
        website="https://x.com", is_independent=True,
        first_seen_at=datetime.now(timezone.utc),
        enriched_at=None, enriched_fields=[],
        mention_count=0, source_names=[],
    )
    assert m.missing_fields == []


def test_missing_fields_all_absent():
    """No key fields populated → all are missing."""
    m = DiscoveredMaker(
        id=1, canonical_name="X", country=None, founded_year=None,
        website=None, is_independent=True,
        first_seen_at=datetime.now(timezone.utc),
        enriched_at=None, enriched_fields=[],
        mention_count=0, source_names=[],
    )
    assert set(m.missing_fields) == {"country", "founded_year", "website"}


def test_field_was_enriched_true():
    """A field listed in enriched_fields should report enriched."""
    m = DiscoveredMaker(
        id=1, canonical_name="X", country="Japan", founded_year=None,
        website="https://x.com", is_independent=True,
        first_seen_at=datetime.now(timezone.utc),
        enriched_at=datetime.now(timezone.utc),
        enriched_fields=["website", "country"],
        mention_count=0, source_names=[],
    )
    assert m.field_was_enriched("website") is True
    assert m.field_was_enriched("country") is True


def test_field_was_enriched_false():
    """A field NOT in enriched_fields should report not enriched."""
    m = DiscoveredMaker(
        id=1, canonical_name="X", country="Japan", founded_year=None,
        website="https://x.com", is_independent=True,
        first_seen_at=datetime.now(timezone.utc),
        enriched_at=datetime.now(timezone.utc),
        enriched_fields=["website"],
        mention_count=0, source_names=[],
    )
    # country is set but not enriched — came from somewhere else (seed/manual)
    assert m.field_was_enriched("country") is False
    assert m.field_was_enriched("founded_year") is False


def test_field_was_enriched_with_none_list():
    """If enriched_fields is None or empty, no field reports enriched."""
    m = DiscoveredMaker(
        id=1, canonical_name="X", country=None, founded_year=None,
        website=None, is_independent=True,
        first_seen_at=datetime.now(timezone.utc),
        enriched_at=None, enriched_fields=[],
        mention_count=0, source_names=[],
    )
    assert m.field_was_enriched("website") is False


# ---------- Route renders ----------


def test_discoveries_route_renders(client):
    """The /discoveries route should return 200 with the rendered page."""
    resp = client.get("/discoveries")
    assert resp.status_code == 200
    assert "Discoveries" in resp.text


def test_discoveries_route_with_some_data(client):
    """When auto-discovered makers exist, they appear in the rendered page."""
    with session_scope() as session:
        m = Maker(
            canonical_name="Notable New Brand",
            country="Japan",
            is_independent=True,
        )
        session.add(m)

    resp = client.get("/discoveries")
    assert resp.status_code == 200
    assert "Notable New Brand" in resp.text


def test_discoveries_empty_state_message(client):
    """With no auto-discovered makers, an empty-state message appears."""
    resp = client.get("/discoveries")
    assert resp.status_code == 200
    # When count == 0, the empty-state copy is shown
    assert "No auto-discovered makers" in resp.text or "None yet" in resp.text
