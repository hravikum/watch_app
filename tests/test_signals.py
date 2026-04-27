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


def test_prestige_caps_awards_per_source(session):
    """A brand with 14 GPHG nominee URLs across 3 editions should NOT score
    14× the prestige of a brand with one nomination per edition.

    This was a real ranking bug: Parmigiani Fleurier accumulated 14 GPHG
    nominee mentions (4 in 2025, 6 in 2024, 4 in 2023) because they enter
    many watches per year, and that drove their prestige_weighted_mentions
    to ~16.8 (14 × 1.2). A maker with one nomination per edition would only
    score 3.6 (3 × 1.2). The cap fixes this by counting each award source
    once regardless of how many mentions came from it.
    """
    maker = Maker(canonical_name="Prolific Brand", aliases=[], is_independent=True)
    session.add(maker)
    session.flush()

    gphg_2025 = SourceRow(name="GPHG 2025", category="awards", prestige_weight=1.2)
    gphg_2024 = SourceRow(name="GPHG 2024", category="awards", prestige_weight=1.2)
    gphg_2023 = SourceRow(name="GPHG 2023", category="awards", prestige_weight=1.2)
    session.add_all([gphg_2025, gphg_2024, gphg_2023])
    session.flush()

    now = datetime.now(timezone.utc)
    # 4 mentions in GPHG 2025, 6 in 2024, 4 in 2023 — 14 total.
    for src, n in [(gphg_2025, 4), (gphg_2024, 6), (gphg_2023, 4)]:
        for i in range(n):
            session.add(Mention(
                maker_id=maker.id, source_id=src.id,
                url=f"https://gphg/{src.name}/{i}",
                title=f"{src.name} watch {i}",
                published_at=now,
            ))
    session.commit()

    # With cap: 3 distinct GPHG editions × 1.2 each = 3.6
    # Without cap (old behavior): 14 × 1.2 = 16.8
    score = compute_prestige_weighted_mentions(session, maker)
    assert abs(score - 3.6) < 1e-9, (
        f"Expected 3.6 (capped), got {score}. The awards cap should count "
        f"each GPHG edition once regardless of how many of this maker's "
        f"watches were nominated."
    )


def test_prestige_does_not_cap_media_sources(session):
    """Media mentions are NOT capped — 4 SJX articles is genuinely 4× the
    press signal of 1 article, since each is a discrete editorial decision.
    The cap applies only to award sources (category='awards')."""
    maker = Maker(canonical_name="Pressed Brand", aliases=[], is_independent=True)
    session.add(maker)
    session.flush()

    sjx = SourceRow(name="SJX", category="media", prestige_weight=1.0)
    session.add(sjx)
    session.flush()

    now = datetime.now(timezone.utc)
    for i in range(4):
        session.add(Mention(
            maker_id=maker.id, source_id=sjx.id,
            url=f"https://sjx/{i}",
            title=f"article {i}",
            published_at=now,
        ))
    session.commit()

    # 4 SJX mentions × 1.0 prestige = 4.0 (uncapped, as expected)
    assert compute_prestige_weighted_mentions(session, maker) == 4.0


def test_prestige_mixed_awards_and_media(session):
    """Verify the cap and uncapped behavior compose correctly when a maker
    has both award and media mentions."""
    maker = Maker(canonical_name="Mixed Brand", aliases=[], is_independent=True)
    session.add(maker)
    session.flush()

    gphg = SourceRow(name="GPHG 2025", category="awards", prestige_weight=1.2)
    sjx = SourceRow(name="SJX", category="media", prestige_weight=1.0)
    session.add_all([gphg, sjx])
    session.flush()

    now = datetime.now(timezone.utc)
    # 3 GPHG nominee mentions (capped to 1 contribution = 1.2)
    for i in range(3):
        session.add(Mention(
            maker_id=maker.id, source_id=gphg.id,
            url=f"https://gphg/{i}", title=f"watch {i}", published_at=now,
        ))
    # 2 SJX articles (uncapped = 2.0)
    for i in range(2):
        session.add(Mention(
            maker_id=maker.id, source_id=sjx.id,
            url=f"https://sjx/{i}", title=f"article {i}", published_at=now,
        ))
    session.commit()

    # Capped GPHG (1.2) + uncapped SJX (2 × 1.0) = 3.2
    assert abs(compute_prestige_weighted_mentions(session, maker) - 3.2) < 1e-9


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
