"""Tests for individual signal calculators.

Each signal is tested in isolation with a tiny hand-built DB. We don't run
the full pipeline — that's in test_orchestrator_with_rss.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from watch_app.scoring.signals import (
    RECENCY_CAP_DAYS,
    VELOCITY_WINDOW_DAYS,
    compute_first_seen_recency,
    compute_independent_flag,
    compute_mention_count,
    compute_obscurity,
    compute_prestige_weighted_mentions,
    compute_source_diversity,
    compute_velocity_90d,
)
from watch_app.storage.models import Maker, Mention
from watch_app.storage.models import Source as SourceRow


@pytest.fixture
def maker_with_mentions(session):
    """Create a maker with three mentions across two sources, varying dates."""
    maker = Maker(canonical_name="Test Maker", aliases=[], is_independent=True)
    session.add(maker)
    session.flush()

    src_a = SourceRow(name="Src A", category="media", prestige_weight=1.0)
    src_b = SourceRow(name="Src B", category="media", prestige_weight=0.5)
    session.add_all([src_a, src_b])
    session.flush()

    now = datetime.now(timezone.utc)
    # Two from Src A (one recent, one old), one from Src B (recent).
    session.add_all([
        Mention(
            maker_id=maker.id,
            source_id=src_a.id,
            url="https://a.example/1",
            title="recent on A",
            published_at=now - timedelta(days=10),
        ),
        Mention(
            maker_id=maker.id,
            source_id=src_a.id,
            url="https://a.example/2",
            title="older on A",
            published_at=now - timedelta(days=120),
        ),
        Mention(
            maker_id=maker.id,
            source_id=src_b.id,
            url="https://b.example/1",
            title="recent on B",
            published_at=now - timedelta(days=30),
        ),
    ])
    session.commit()
    return maker


def test_mention_count(session, maker_with_mentions):
    assert compute_mention_count(session, maker_with_mentions) == 3.0


def test_mention_count_zero_for_unmentioned_maker(session):
    m = Maker(canonical_name="Nobody", aliases=[])
    session.add(m)
    session.flush()
    assert compute_mention_count(session, m) == 0.0


def test_prestige_weighted_mentions(session, maker_with_mentions):
    # Src A (1.0) x 2 + Src B (0.5) x 1 = 2.5
    assert compute_prestige_weighted_mentions(session, maker_with_mentions) == 2.5


def test_source_diversity(session, maker_with_mentions):
    # Two distinct sources, regardless of how many mentions from each.
    assert compute_source_diversity(session, maker_with_mentions) == 2.0


def test_velocity_90d(session, maker_with_mentions):
    # Of the three mentions, two are within 90 days (10d, 30d); one at 120d is not.
    assert compute_velocity_90d(session, maker_with_mentions) == 2.0


def test_first_seen_recency_clamped_at_cap(session):
    """Mention older than RECENCY_CAP_DAYS → 0.0."""
    m = Maker(canonical_name="Old", aliases=[])
    src = SourceRow(name="S", category="media", prestige_weight=0.5)
    session.add_all([m, src])
    session.flush()
    session.add(
        Mention(
            maker_id=m.id,
            source_id=src.id,
            url="https://x/1",
            published_at=datetime.now(timezone.utc) - timedelta(days=RECENCY_CAP_DAYS + 10),
        )
    )
    session.commit()
    assert compute_first_seen_recency(session, m) == 0.0


def test_first_seen_recency_recent(session):
    """Very recent mention → near 1.0."""
    m = Maker(canonical_name="New", aliases=[])
    src = SourceRow(name="S", category="media", prestige_weight=0.5)
    session.add_all([m, src])
    session.flush()
    session.add(
        Mention(
            maker_id=m.id,
            source_id=src.id,
            url="https://x/1",
            published_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    session.commit()
    value = compute_first_seen_recency(session, m)
    # 1 day / 180 day cap ≈ 0.006 lost. Should be very close to 1.0.
    assert 0.99 <= value <= 1.0


def test_first_seen_recency_zero_without_mentions(session):
    m = Maker(canonical_name="Nobody", aliases=[])
    session.add(m)
    session.flush()
    assert compute_first_seen_recency(session, m) == 0.0


def test_independent_flag(session):
    indie = Maker(canonical_name="Indie", aliases=[], is_independent=True)
    big = Maker(canonical_name="BigCorp", aliases=[], is_independent=False)
    session.add_all([indie, big])
    session.flush()
    assert compute_independent_flag(session, indie) == 1.0
    assert compute_independent_flag(session, big) == 0.0


def test_obscurity_peaks_at_zero_mentions(session):
    """A maker with no mentions should get obscurity = 1.0 (maximum)."""
    m = Maker(canonical_name="Nobody", aliases=[])
    session.add(m)
    session.flush()
    assert compute_obscurity(session, m) == 1.0


def test_obscurity_decays_with_mentions(session, maker_with_mentions):
    """3 mentions → obscurity = 1 / (1 + 3) = 0.25."""
    assert compute_obscurity(session, maker_with_mentions) == pytest.approx(0.25)


def test_obscurity_monotonic(session):
    """More mentions → strictly less obscurity."""
    src = SourceRow(name="S", category="media", prestige_weight=0.5)
    m = Maker(canonical_name="Growing", aliases=[])
    session.add_all([src, m])
    session.flush()

    obs_vals = []
    for i in range(5):
        session.add(
            Mention(
                maker_id=m.id,
                source_id=src.id,
                url=f"https://x/{i}",
                published_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        session.flush()
        obs_vals.append(compute_obscurity(session, m))

    # Each addition strictly decreases obscurity.
    for earlier, later in zip(obs_vals, obs_vals[1:]):
        assert later < earlier
