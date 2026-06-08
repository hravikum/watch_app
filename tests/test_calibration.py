"""Tests for the source-quality calibration diagnostic (scoring/calibration.py).

The diagnostic is read-only and feeds prestige-weight tuning. The invariants
that matter:
  - first_surfaced counts ALL non-suppressed makers (seed AND discovered) — a
    source being first to cover even a curated maker is a real signal. (3Z: an
    earlier version wrongly excluded seed makers and zeroed the column on
    seed-dominated data; test_seed_makers_count_as_first_surfaced pins that.)
  - awards-category sources are excluded from the first race; their makers are
    credited to the earliest non-awards source instead (or to nobody if the
    maker is awards-only)
  - first_surfaced is attributed to the earliest source by discovered_at
  - suppressed makers are excluded from every count
  - first_ratio == first_surfaced / total_mentions (0.0 when no mentions)
  - a window restricts counts to that window
  - zero-mention sources still appear

"Simon Brette" is a real seed canonical_name (watch_app/data/known_makers.yaml).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from watch_app.scoring.calibration import compute_source_quality
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Source as SourceRow


SEED_NAME = "Simon Brette"  # present in the seed YAML


def _rows_by_name(rows):
    return {r.source_name: r for r in rows}


def _seed_population():
    """Source A (media) first-mentions a seed maker + is earliest on Discovered
    One. Source B (community) re-covers Discovered One later and mentions the
    suppressed Discovered Two. GPHG (awards) is earliest on Discovered One of
    all, but must be excluded from the first race."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        a = SourceRow(name="Source A", category="media", prestige_weight=1.0)
        b = SourceRow(name="Source B", category="community", prestige_weight=0.5)
        c = SourceRow(name="Silent Source", category="media", prestige_weight=0.4)
        gphg = SourceRow(name="GPHG 2025", category="awards", prestige_weight=1.2)
        session.add_all([a, b, c, gphg])
        session.flush()

        seed = Maker(canonical_name=SEED_NAME, is_independent=True)
        disc1 = Maker(canonical_name="Discovered One", is_independent=True)
        disc2 = Maker(
            canonical_name="Discovered Two",
            is_independent=True,
            is_suppressed=True,
        )
        session.add_all([seed, disc1, disc2])
        session.flush()

        # GPHG mentions disc1 EARLIEST of all — but awards are out of the race,
        # so disc1's first credit must go to Source A (the earliest media src).
        session.add(Mention(
            maker_id=disc1.id, source_id=gphg.id, url="https://x/d1-gphg",
            discovered_at=now - timedelta(days=5),
        ))
        # Source A: first mention of the seed maker + earliest MEDIA mention of disc1.
        session.add(Mention(
            maker_id=seed.id, source_id=a.id, url="https://x/seed-a",
            discovered_at=now,
        ))
        session.add(Mention(
            maker_id=disc1.id, source_id=a.id, url="https://x/d1-a",
            discovered_at=now,
        ))
        # Source B: later mention of disc1 + the suppressed disc2.
        session.add(Mention(
            maker_id=disc1.id, source_id=b.id, url="https://x/d1-b",
            discovered_at=now + timedelta(days=2),
        ))
        session.add(Mention(
            maker_id=disc2.id, source_id=b.id, url="https://x/d2-b",
            discovered_at=now,
        ))

        return {"a": a.id, "b": b.id, "seed": seed.id, "d1": disc1.id}


def test_seed_makers_count_as_first_surfaced(test_engine):
    """The 3Z regression: a source first to cover a SEED maker earns the credit.
    A seed-only population must NOT produce an all-zero first column."""
    _seed_population()
    with session_scope() as session:
        rows = _rows_by_name(compute_source_quality(session))

    # Source A is first on the seed maker AND on disc1 (earliest media) → 2.
    assert rows["Source A"].first_surfaced == 2


def test_awards_excluded_from_first_race(test_engine):
    """GPHG mentioned disc1 earliest of all, but awards are out of the race, so
    GPHG gets no first credit and disc1 falls to the earliest media source."""
    _seed_population()
    with session_scope() as session:
        rows = _rows_by_name(compute_source_quality(session))

    assert rows["GPHG 2025"].first_surfaced == 0
    # disc1 credited to Source A (earliest media), not GPHG (earlier but awards).
    assert rows["Source A"].first_surfaced == 2  # seed + disc1


def test_awards_only_maker_credited_to_nobody(test_engine):
    """A maker mentioned only by an awards source is surfaced-first by no one."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        gphg = SourceRow(name="GPHG 2024", category="awards", prestige_weight=1.2)
        media = SourceRow(name="Media One", category="media", prestige_weight=0.8)
        session.add_all([gphg, media]); session.flush()
        only = Maker(canonical_name="Awards Only Maker", is_independent=True)
        session.add(only); session.flush()
        session.add(Mention(
            maker_id=only.id, source_id=gphg.id, url="https://x/ao",
            discovered_at=now,
        ))

    with session_scope() as session:
        rows = compute_source_quality(session)
        assert sum(r.first_surfaced for r in rows) == 0


def test_first_surfaced_goes_to_earliest_source(test_engine):
    _seed_population()
    with session_scope() as session:
        rows = _rows_by_name(compute_source_quality(session))

    # disc1: A (day 0) before B (day 2) among media; A wins.
    assert rows["Source B"].first_surfaced == 0


def test_suppressed_makers_excluded_from_all_counts(test_engine):
    _seed_population()
    with session_scope() as session:
        rows = _rows_by_name(compute_source_quality(session))

    # Source B's mentions: disc1 (counts) + suppressed disc2 (excluded).
    assert rows["Source B"].total_mentions == 1
    assert rows["Source B"].distinct_makers == 1
    assert rows["Source B"].first_surfaced == 0


def test_first_ratio_is_first_over_total(test_engine):
    _seed_population()
    with session_scope() as session:
        rows = _rows_by_name(compute_source_quality(session))

    # Source A: 2 mentions (seed + disc1), 2 first-surfaced → ratio 1.0.
    a = rows["Source A"]
    assert a.total_mentions == 2
    assert a.first_surfaced == 2
    assert a.first_ratio == 1.0


def test_zero_mention_source_appears_with_zeros(test_engine):
    _seed_population()
    with session_scope() as session:
        rows = _rows_by_name(compute_source_quality(session))

    silent = rows["Silent Source"]
    assert silent.total_mentions == 0
    assert silent.distinct_makers == 0
    assert silent.first_surfaced == 0
    assert silent.first_ratio == 0.0


def test_window_restricts_counts(test_engine):
    """A tight window drops mentions older than the cutoff."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        src = SourceRow(name="Windowed", category="media", prestige_weight=1.0)
        session.add(src); session.flush()
        m = Maker(canonical_name="Recent Maker", is_independent=True)
        old = Maker(canonical_name="Old Maker", is_independent=True)
        session.add_all([m, old]); session.flush()
        session.add(Mention(
            maker_id=m.id, source_id=src.id, url="https://x/recent",
            discovered_at=now - timedelta(days=1),
        ))
        session.add(Mention(
            maker_id=old.id, source_id=src.id, url="https://x/old",
            discovered_at=now - timedelta(days=40),
        ))

    with session_scope() as session:
        all_time = _rows_by_name(compute_source_quality(session))
        last_week = _rows_by_name(compute_source_quality(session, window_days=7))

    assert all_time["Windowed"].total_mentions == 2
    assert all_time["Windowed"].first_surfaced == 2
    assert last_week["Windowed"].total_mentions == 1
    assert last_week["Windowed"].first_surfaced == 1


def test_first_surfaced_totals_equal_eligible_maker_count(test_engine):
    """Sum of first_surfaced == non-suppressed makers with a non-awards mention,
    each attributed exactly once. Here: seed maker + disc1 = 2 (disc2 suppressed,
    no awards-only makers)."""
    _seed_population()
    with session_scope() as session:
        rows = compute_source_quality(session)
        assert sum(r.first_surfaced for r in rows) == 2
