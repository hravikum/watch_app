"""Tests for the taste / character dimensions (chunk 3A, Layer 1a).

Three layers of coverage:
- the controlled-vocabulary validator (unit),
- the committed seed YAML actually conforming to it (data guard),
- the seed loader writing the fields and the detail page rendering them.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app import taste
from watch_app.api.routers.makers import router as makers_router
from watch_app.extraction.seed import _load_yaml, load_seed_makers
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


# ---- Validator ----


def test_validate_accepts_valid_entry():
    entry = {
        "canonical_name": "Test Maker",
        "aesthetic_tags": ["classical", "dressy"],
        "maker_type": "solo-watchmaker",
        "price_band": "haute",
        "complication_focus": ["time-only"],
        "movement_origin": "in-house",
    }
    taste.validate_taste_fields(entry)  # should not raise


def test_validate_ignores_absent_taste_fields():
    """An entry with no taste fields at all is fine — taste is optional."""
    taste.validate_taste_fields({"canonical_name": "No Tags", "country": "France"})


def test_validate_rejects_unknown_multi_value_tag():
    entry = {"canonical_name": "Bad", "aesthetic_tags": ["classical", "steampunk"]}
    with pytest.raises(ValueError, match="aesthetic_tags"):
        taste.validate_taste_fields(entry)


def test_validate_rejects_unknown_single_value():
    entry = {"canonical_name": "Bad", "price_band": "cheap"}
    with pytest.raises(ValueError, match="price_band"):
        taste.validate_taste_fields(entry)


def test_validate_rejects_wrong_type_for_multi_field():
    """A multi-value field given a bare string (not a list) is a mistake."""
    entry = {"canonical_name": "Bad", "aesthetic_tags": "classical"}
    with pytest.raises(ValueError, match="must be a list"):
        taste.validate_taste_fields(entry)


def test_field_label_defined_for_every_taste_field():
    for field in taste.TASTE_FIELDS:
        assert field in taste.FIELD_LABELS


# ---- Data guard: the real YAML must conform ----


def test_committed_seed_yaml_is_all_in_vocabulary():
    """Every entry in known_makers.yaml validates. A typo in a tag fails here
    rather than silently fragmenting the vocabulary."""
    for entry in _load_yaml():
        taste.validate_taste_fields(entry)


def test_seed_yaml_has_tagged_makers():
    """Sanity: the 1a tagging pass actually populated some makers."""
    tagged = [
        e
        for e in _load_yaml()
        if any(e.get(f) is not None for f in taste.TASTE_FIELDS)
    ]
    assert len(tagged) >= 30


# ---- Seed loader writes the fields ----


def test_seed_loads_taste_fields(session):
    load_seed_makers(session)
    session.commit()

    journe = session.query(Maker).filter_by(canonical_name="F.P. Journe").one()
    assert journe.maker_type == "established-indie"
    assert journe.price_band == "haute"
    assert "classical" in journe.aesthetic_tags
    assert "tourbillon" in journe.complication_focus
    assert journe.movement_origin == "in-house"


def test_seed_leaves_untagged_makers_null(session):
    """A maker present in the YAML without taste fields keeps them None."""
    load_seed_makers(session)
    session.commit()

    # Rolex is in the seed for extractor disambiguation only — untagged.
    rolex = session.query(Maker).filter_by(canonical_name="Rolex").one()
    assert rolex.maker_type is None
    assert rolex.aesthetic_tags is None


def test_seed_taste_is_idempotent(session):
    load_seed_makers(session)
    session.commit()
    first = session.query(Maker).filter_by(canonical_name="MB&F").one()
    first_aesthetic = list(first.aesthetic_tags)

    load_seed_makers(session)
    session.commit()
    second = session.query(Maker).filter_by(canonical_name="MB&F").one()
    assert second.aesthetic_tags == first_aesthetic


# ---- Detail page renders the profile ----


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def test_detail_page_renders_profile_for_tagged_maker(client):
    with session_scope() as session:
        m = Maker(
            canonical_name="Tagged Maker",
            is_independent=True,
            maker_type="solo-watchmaker",
            price_band="haute",
            aesthetic_tags=["classical"],
            complication_focus=["time-only"],
            movement_origin="in-house",
        )
        session.add(m)
        session.flush()
        maker_id = m.id

    resp = client.get(f"/makers/{maker_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Profile" in body
    # Display labels, not raw tokens.
    assert "Solo watchmaker" in body
    assert "In-house" in body
    assert "Haute ($50k+)" in body


def test_detail_page_omits_profile_for_untagged_maker(client):
    with session_scope() as session:
        m = Maker(canonical_name="Plain Maker", is_independent=True)
        session.add(m)
        session.flush()
        maker_id = m.id

    resp = client.get(f"/makers/{maker_id}")
    assert resp.status_code == 200
    # No profile panel heading when there's nothing to show.
    assert "Profile" not in resp.text
