"""Tests for "makers like this" similarity (Layer 2b, chunk 3K)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app import taste
from watch_app.api.routers.makers import (
    _shared_labels,
    _similar_makers,
    router as makers_router,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


# ---------- taste.similarity ----------


def test_identical_taste_is_one():
    a = {"maker_type": "solo-watchmaker", "aesthetic_tags": ["classical"]}
    assert taste.similarity(a, dict(a)) == 1.0


def test_no_shared_dimensions_is_none():
    a = {"maker_type": "solo-watchmaker"}
    b = {"price_band": "mid"}
    assert taste.similarity(a, b) is None


def test_disjoint_single_value_is_zero():
    a = {"maker_type": "solo-watchmaker"}
    b = {"maker_type": "microbrand"}
    assert taste.similarity(a, b) == 0.0


def test_jaccard_on_multi_value():
    a = {"aesthetic_tags": ["tool", "modern"]}
    b = {"aesthetic_tags": ["tool"]}
    # intersection 1 / union 2 = 0.5; sole shared dim, so weight cancels.
    assert taste.similarity(a, b) == 0.5


def test_high_weight_match_beats_low_weight_match():
    # maker_type (weight 2) vs price_band (weight 1).
    high = taste.similarity(
        {"maker_type": "solo-watchmaker", "price_band": "mid"},
        {"maker_type": "solo-watchmaker", "price_band": "high"},
    )  # high-weight matches, low-weight mismatches → 2/3
    low = taste.similarity(
        {"maker_type": "solo-watchmaker", "price_band": "mid"},
        {"maker_type": "microbrand", "price_band": "mid"},
    )  # high-weight mismatches, low-weight matches → 1/3
    assert high > low
    assert round(high, 3) == 0.667
    assert round(low, 3) == 0.333


def test_sparse_maker_not_penalized():
    # B has an extra populated dimension A lacks; only the shared one counts.
    a = {"maker_type": "solo-watchmaker"}
    b = {"maker_type": "solo-watchmaker", "price_band": "high"}
    assert taste.similarity(a, b) == 1.0


# ---------- _similar_makers ----------


def _add(name, **kw) -> int:
    with session_scope() as session:
        m = Maker(canonical_name=name, **kw)
        session.add(m)
        session.flush()
        return m.id


def test_similar_ranks_closer_maker_first(test_engine):
    target = _add(
        "Target",
        aesthetic_tags=["classical"],
        maker_type="solo-watchmaker",
        movement_origin="in-house",
    )
    _add(
        "Twin",
        aesthetic_tags=["classical"],
        maker_type="solo-watchmaker",
        movement_origin="in-house",
    )
    _add("Cousin", aesthetic_tags=["classical"], maker_type="microbrand")
    _add("Stranger", aesthetic_tags=["tool"], maker_type="established-indie")

    out = _similar_makers(target, limit=6)
    names = [s["name"] for s in out]
    assert names[0] == "Twin"            # most overlap ranks first
    assert "Cousin" in names             # partial overlap included
    assert "Stranger" not in names       # zero overlap on shared dims → excluded
    assert out[0]["match"] == 100
    assert "Classical" in out[0]["shared"]


def test_similar_empty_when_target_untagged(test_engine):
    target = _add("Bare")
    _add("Tagged", maker_type="solo-watchmaker")
    assert _similar_makers(target) == []


def test_shared_labels_human_readable():
    a = {"aesthetic_tags": ["classical", "modern"], "maker_type": "solo-watchmaker"}
    b = {"aesthetic_tags": ["classical"], "maker_type": "solo-watchmaker"}
    labels = _shared_labels(a, b)
    assert "Classical" in labels
    assert "Solo watchmaker" in labels
    assert "Modern" not in labels  # not shared


# ---------- detail page section ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def test_detail_page_shows_similar_section(client):
    target = _add(
        "Target", aesthetic_tags=["classical"], maker_type="solo-watchmaker"
    )
    _add("Twin", aesthetic_tags=["classical"], maker_type="solo-watchmaker")
    r = client.get(f"/makers/{target}")
    assert r.status_code == 200
    assert "Similar makers" in r.text
    assert "Twin" in r.text


def test_detail_page_no_section_when_no_similar(client):
    target = _add("Lonely", aesthetic_tags=["tool"])
    _add("Different", aesthetic_tags=["dressy"], maker_type="microbrand")
    r = client.get(f"/makers/{target}")
    assert r.status_code == 200
    # No maker shares overlapping taste → section omitted.
    assert "Similar makers" not in r.text
