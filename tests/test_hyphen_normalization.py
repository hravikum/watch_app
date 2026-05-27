"""Tests for hyphen normalization in 2W-hyphen-norm.

Verifies that hyphen-vs-space variants of maker names match correctly,
both in the rule-based extractor (entities.py) and in the resolver
(resolver.py).

Motivating case: Hodinkee article "Introducing: Petermann-Bédat 1825
Hour Glass" was silently dropped because the seed entry "Petermann
Bédat" (space) didn't match the article's "Petermann-Bédat" (hyphen).
"""
from __future__ import annotations

from watch_app.extraction.entities import (
    _normalize as entities_normalize,
    extract_maker_names,
    invalidate_vocab_cache,
)
from watch_app.extraction.resolver import (
    _normalize as resolver_normalize,
    resolve_or_create_maker,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


# ---------- _normalize unit tests ----------


def test_entities_normalize_collapses_hyphen():
    assert entities_normalize("Petermann-Bédat") == "petermann bedat"


def test_entities_normalize_collapses_multiple_hyphens():
    assert entities_normalize("A--B---C") == "a b c"


def test_entities_normalize_preserves_space_form():
    # The space form should produce the same output as the hyphen form,
    # so vocab built from seed matches against article text.
    assert entities_normalize("Petermann Bédat") == entities_normalize("Petermann-Bédat")


def test_entities_normalize_mixed_hyphen_and_space():
    # Multiple separators of mixed kinds collapse to one space.
    assert entities_normalize("Lang - und  - Heyne") == "lang und heyne"


def test_entities_normalize_preserves_other_punctuation():
    # We deliberately only collapse hyphens and whitespace.
    # Periods, ampersands, etc. stay as-is.
    assert entities_normalize("F.P. Journe") == "f.p. journe"
    assert entities_normalize("MB&F") == "mb&f"


def test_resolver_normalize_collapses_hyphen():
    assert resolver_normalize("Petermann-Bédat") == "petermann bedat"


def test_entities_and_resolver_normalize_agree():
    # Both functions must produce the same output for the same input,
    # or the resolver won't match what the extractor produces.
    samples = [
        "Petermann-Bédat",
        "Petermann Bédat",
        "Raúl Pagès",
        "Lang & Heyne",
        "A. Lange & Söhne",
        "F.P. Journe",
    ]
    for s in samples:
        assert entities_normalize(s) == resolver_normalize(s), f"mismatch on {s!r}"


# ---------- End-to-end extractor tests ----------


def test_extractor_matches_hyphen_variant_against_space_seed(test_engine):
    """The motivating case: seed has "Petermann Bédat" with space,
    article title uses "Petermann-Bédat" with hyphen. Should match."""
    with session_scope() as session:
        session.add(Maker(
            canonical_name="Petermann Bédat",
            aliases=["Petermann Bedat"],
            is_independent=True,
        ))
        session.flush()

    invalidate_vocab_cache()

    with session_scope() as session:
        names = extract_maker_names(
            session,
            title="Introducing: Petermann-Bédat 1825 Hour Glass",
            text="",
        )
    assert names == ["Petermann Bédat"]


def test_extractor_matches_space_variant_against_space_seed(test_engine):
    """Sanity: the original (no-hyphen) form still works."""
    with session_scope() as session:
        session.add(Maker(
            canonical_name="Petermann Bédat",
            aliases=["Petermann Bedat"],
            is_independent=True,
        ))
        session.flush()

    invalidate_vocab_cache()

    with session_scope() as session:
        names = extract_maker_names(
            session,
            title="Introducing: Petermann Bédat 1825 Hour Glass",
            text="",
        )
    assert names == ["Petermann Bédat"]


# ---------- End-to-end resolver tests ----------


def test_resolver_resolves_hyphen_variant_to_space_canonical(test_engine):
    """Given an existing Maker with space-separated canonical_name,
    a hyphen-separated candidate name resolves to it."""
    with session_scope() as session:
        session.add(Maker(
            canonical_name="Petermann Bédat",
            aliases=["Petermann Bedat"],
            is_independent=True,
        ))
        session.flush()

    with session_scope() as session:
        maker = resolve_or_create_maker(session, "Petermann-Bédat")
    assert maker.canonical_name == "Petermann Bédat"


def test_resolver_does_not_create_duplicate_for_hyphen_variant(test_engine):
    """Resolving a hyphen variant should NOT create a second Maker row."""
    with session_scope() as session:
        session.add(Maker(
            canonical_name="Petermann Bédat",
            aliases=["Petermann Bedat"],
            is_independent=True,
        ))
        session.flush()

    with session_scope() as session:
        _ = resolve_or_create_maker(session, "Petermann-Bédat")

    with session_scope() as session:
        count = session.query(Maker).filter(
            Maker.canonical_name.like("%Petermann%")
        ).count()
    assert count == 1
