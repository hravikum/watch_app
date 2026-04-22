"""Tests for the rule-based entity extractor."""
from __future__ import annotations

import pytest

from watch_app.extraction.entities import (
    extract_maker_names,
    invalidate_vocab_cache,
)
from watch_app.extraction.seed import load_seed_makers


@pytest.fixture(autouse=True)
def _reset_vocab():
    """Ensure each test starts with a clean vocab cache."""
    invalidate_vocab_cache()
    yield
    invalidate_vocab_cache()


@pytest.fixture
def seeded(session):
    load_seed_makers(session)
    session.commit()
    invalidate_vocab_cache()
    return session


def test_finds_canonical_name_in_title(seeded):
    names = extract_maker_names(
        seeded,
        title="Simon Brette's latest release draws attention",
        text="",
    )
    assert "Simon Brette" in names


def test_finds_multiple_makers_in_body(seeded):
    names = extract_maker_names(
        seeded,
        title="Independent horology roundup",
        text=(
            "This week we look at new pieces from Voutilainen, "
            "Petermann Bédat and F.P. Journe."
        ),
    )
    # "Voutilainen" is an alias for "Kari Voutilainen" — both should map to canonical.
    assert "Kari Voutilainen" in names
    assert "Petermann Bédat" in names
    assert "F.P. Journe" in names


def test_diacritic_insensitive_match(seeded):
    names = extract_maker_names(
        seeded,
        title="Raul Pages wins the prize",  # no diacritics
        text="",
    )
    assert "Raúl Pagès" in names


def test_alias_matches_to_canonical(seeded):
    # "Akrivia" is an alias for "Atelier Akrivia"
    names = extract_maker_names(
        seeded,
        title="Inside Akrivia's atelier",
        text="",
    )
    assert "Atelier Akrivia" in names


def test_case_insensitive(seeded):
    names = extract_maker_names(
        seeded,
        title="Why NOMOS matters",
        text="",
    )
    assert "Nomos" in names


def test_does_not_match_substring(seeded):
    """'MB' shouldn't match inside 'MBA' or similar."""
    names = extract_maker_names(
        seeded,
        title="She got her MBA last year",
        text="",
    )
    # MB&F is in the seed list, but "MBA" shouldn't trigger it.
    assert "MB&F" not in names


def test_no_false_positives_on_empty(seeded):
    names = extract_maker_names(seeded, title="", text="")
    assert names == []


def test_returns_makers_in_occurrence_order(seeded):
    names = extract_maker_names(
        seeded,
        title="",
        text="First we cover F.P. Journe, then Voutilainen, finally Urwerk.",
    )
    # Order should follow first occurrence in text.
    assert names.index("F.P. Journe") < names.index("Kari Voutilainen")
    assert names.index("Kari Voutilainen") < names.index("Urwerk")


def test_longer_name_wins_over_shorter_overlap(seeded):
    """'Petermann Bédat' should match, not 'Bédat' alone if that were an alias."""
    names = extract_maker_names(
        seeded,
        title="New from Petermann Bédat",
        text="",
    )
    assert "Petermann Bédat" in names
