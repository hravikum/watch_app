"""Tests for the maker resolver."""
from __future__ import annotations

from watch_app.extraction.resolver import resolve_or_create_maker
from watch_app.storage.models import Maker


def test_creates_new_maker(session):
    maker = resolve_or_create_maker(session, "Simon Brette")
    assert maker.id is not None
    assert maker.canonical_name == "Simon Brette"
    assert session.query(Maker).count() == 1


def test_exact_match_returns_existing(session):
    a = resolve_or_create_maker(session, "Simon Brette")
    session.commit()
    b = resolve_or_create_maker(session, "Simon Brette")
    assert a.id == b.id
    assert session.query(Maker).count() == 1


def test_diacritic_insensitive_match(session):
    a = resolve_or_create_maker(session, "Raúl Pagès")
    session.commit()
    b = resolve_or_create_maker(session, "Raul Pages")
    # Diacritics and case are normalized away, so these match exactly.
    assert a.id == b.id
    assert session.query(Maker).count() == 1


def test_alias_learned_on_fuzzy_match(session):
    """When a new spelling only matches fuzzily, it's recorded as an alias."""
    a = resolve_or_create_maker(session, "Petermann Bédat")
    session.commit()
    # Slight typo — same maker, but normalization won't collapse them.
    b = resolve_or_create_maker(session, "Peterman Bedat")
    assert a.id == b.id
    session.commit()
    # Re-fetch to confirm persistence.
    session.expire_all()
    refreshed = session.get(Maker, a.id)
    assert "Peterman Bedat" in (refreshed.aliases or [])


def test_fuzzy_whitespace_match(session):
    a = resolve_or_create_maker(session, "Petermann Bédat")
    session.commit()
    b = resolve_or_create_maker(session, "Petermann Bédat ")  # trailing space
    assert a.id == b.id


def test_different_names_do_not_match(session):
    a = resolve_or_create_maker(session, "Simon Brette")
    session.commit()
    b = resolve_or_create_maker(session, "Daizoh Makihara")
    assert a.id != b.id
    assert session.query(Maker).count() == 2
