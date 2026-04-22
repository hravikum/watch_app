"""Tests for the shared scoring queries.

Focus: fetch_top_makers must return exactly one row per maker, using the
latest scoring run for that preset. This was a real bug in an earlier cut
of the CLI and web UI — the first tests I wrote didn't catch it because
they only ran the pipeline once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from watch_app.scoring.queries import fetch_top_makers
from watch_app.storage.models import Maker, Score


def test_returns_latest_score_per_maker(session):
    """Two scoring runs for the same maker — only the latest should appear."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=1)

    m = Maker(canonical_name="Test Maker", aliases=[], country="Testland")
    session.add(m)
    session.flush()

    # Old score from a prior scan.
    session.add(
        Score(
            maker_id=m.id,
            preset="rising_star",
            value=1.0,
            rank=5,
            computed_at=earlier,
        )
    )
    # New score from the latest scan.
    session.add(
        Score(
            maker_id=m.id,
            preset="rising_star",
            value=3.0,
            rank=1,
            computed_at=now,
        )
    )
    session.commit()

    rows = fetch_top_makers(session, "rising_star", limit=10)

    # Exactly one row for this maker, with the *latest* values.
    matching = [r for r in rows if r["canonical_name"] == "Test Maker"]
    assert len(matching) == 1, f"expected 1 row per maker, got {len(matching)}"
    assert matching[0]["score"] == 3.0
    assert matching[0]["rank"] == 1


def test_ignores_other_presets(session):
    """A maker scored under hidden_gem shouldn't appear in rising_star results."""
    now = datetime.now(timezone.utc)
    m = Maker(canonical_name="Only In Hidden Gem", aliases=[])
    session.add(m)
    session.flush()
    session.add(
        Score(
            maker_id=m.id,
            preset="hidden_gem",
            value=2.0,
            rank=1,
            computed_at=now,
        )
    )
    session.commit()

    rows = fetch_top_makers(session, "rising_star", limit=20)
    names = [r["canonical_name"] for r in rows]
    assert "Only In Hidden Gem" not in names


def test_empty_when_no_scores(session):
    """Empty DB → empty list, not a crash."""
    rows = fetch_top_makers(session, "rising_star", limit=20)
    assert rows == []


def test_includes_mention_count(session):
    """Sanity: the row includes mention_count (0 if no mentions yet)."""
    now = datetime.now(timezone.utc)
    m = Maker(canonical_name="No Mentions Yet", aliases=[])
    session.add(m)
    session.flush()
    session.add(
        Score(
            maker_id=m.id, preset="rising_star", value=0, rank=1, computed_at=now
        )
    )
    session.commit()

    rows = fetch_top_makers(session, "rising_star", limit=1)
    assert rows[0]["mention_count"] == 0
