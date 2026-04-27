"""Tests for the methodology page's worked-example reconstruction.

Key invariant: the worked example's final score must match the ranker's
composite score for the same maker. These are computed from the same raw
signals via the same math, so if they drift apart, either the ranker or
the methodology helper is wrong.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.scoring.methodology import (
    SIGNAL_DESCRIPTIONS,
    SourceContribution,
    build_source_contributions,
    build_worked_example,
)
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.ranker import rank_all_makers
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Source as SourceRow


# ---------- Fixtures ----------


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


def _seed_diverse_population():
    """Seed a handful of makers with varying signal profiles so z-scores
    actually have spread. Using 6 makers gives us a reasonable population
    for normalization to mean something."""
    with session_scope() as session:
        sjx = SourceRow(name="SJX", category="media", prestige_weight=1.0)
        mono = SourceRow(name="Monochrome", category="media", prestige_weight=0.9)
        gphg = SourceRow(name="GPHG 2025", category="awards", prestige_weight=1.2)
        session.add_all([sjx, mono, gphg])
        session.flush()

        makers = [
            # Popular indie with diverse coverage
            Maker(canonical_name="Popular Indie", is_independent=True,
                  country="Switzerland"),
            # Quietly prestigious — 1 GPHG only
            Maker(canonical_name="Quiet Gem", is_independent=True,
                  country="France"),
            # Fully obscure indie
            Maker(canonical_name="Obscure Maker", is_independent=True,
                  country="Japan"),
            # Conglomerate with lots of coverage
            Maker(canonical_name="Big Conglomerate", is_independent=False,
                  country="Switzerland"),
            # Mid indie
            Maker(canonical_name="Mid Indie", is_independent=True,
                  country="Germany"),
            # Another obscure (tie situation)
            Maker(canonical_name="Second Obscure", is_independent=True,
                  country="USA"),
        ]
        session.add_all(makers)
        session.flush()
        mids = {m.canonical_name: m.id for m in makers}

        now = datetime(2025, 10, 1, tzinfo=timezone.utc)
        # Popular Indie: 3 mentions across 3 sources
        for i, src in enumerate([sjx, mono, gphg]):
            session.add(Mention(
                maker_id=mids["Popular Indie"], source_id=src.id,
                url=f"https://x/pop-{i}", title=f"coverage {i}",
                published_at=now,
            ))
        # Quiet Gem: 1 GPHG mention
        session.add(Mention(
            maker_id=mids["Quiet Gem"], source_id=gphg.id,
            url="https://x/quiet-1", title="GPHG nom", published_at=now,
        ))
        # Mid Indie: 2 SJX mentions
        for i in range(2):
            session.add(Mention(
                maker_id=mids["Mid Indie"], source_id=sjx.id,
                url=f"https://x/mid-{i}", title=f"mid {i}", published_at=now,
            ))
        # Big Conglomerate: 4 mentions (lots of press but it's filtered)
        for i, src in enumerate([sjx, mono, gphg, sjx]):
            session.add(Mention(
                maker_id=mids["Big Conglomerate"], source_id=src.id,
                url=f"https://x/big-{i}", title=f"big {i}", published_at=now,
            ))
        # Obscure Maker, Second Obscure: zero mentions

        rank_all_makers(session, "rising_star", normalize=True)
        rank_all_makers(session, "hidden_gem", normalize=True)

        return mids


# ---------- Invariant tests: worked example matches ranker ----------


def test_worked_example_final_score_matches_ranker(test_engine):
    """The most important test: the worked example's final_score must equal
    the Score.value that the ranker stored for the same maker under the same
    preset. If these disagree, the methodology page is lying to users."""
    from watch_app.storage.models import Score

    _seed_diverse_population()

    for preset_name in PRESETS:
        with session_scope() as session:
            example = build_worked_example(session, preset_name)
            assert example is not None, f"no example for {preset_name}"

            # Find the Score row the ranker wrote for this maker+preset.
            score_row = (
                session.query(Score)
                .filter(
                    Score.maker_id == example.maker_id,
                    Score.preset == preset_name,
                )
                .order_by(Score.computed_at.desc())
                .first()
            )
            assert score_row is not None

            # Allow tiny float drift (both paths use the same math so the
            # difference should be at the noise floor).
            assert abs(example.final_score - score_row.value) < 1e-9, (
                f"preset={preset_name} maker={example.maker_name}: "
                f"example.final_score={example.final_score} != "
                f"stored score={score_row.value}"
            )


def test_worked_example_picks_top_ranked_maker(test_engine):
    """The worked example should be built for the #1 maker under each preset."""
    from watch_app.storage.models import Score

    _seed_diverse_population()

    for preset_name in PRESETS:
        with session_scope() as session:
            example = build_worked_example(session, preset_name)
            assert example is not None

            # Find rank 1 under this preset — that's who the example should be about.
            top_score = (
                session.query(Score)
                .filter(Score.preset == preset_name, Score.rank == 1)
                .order_by(Score.computed_at.desc())
                .first()
            )
            assert top_score is not None
            assert example.maker_id == top_score.maker_id, (
                f"worked example for {preset_name} is not the top-ranked maker"
            )


def test_rising_star_example_is_an_independent(test_engine):
    """Under rising_star, the top maker must be independent — the
    multiplier gate would zero out a conglomerate."""
    _seed_diverse_population()

    with session_scope() as session:
        example = build_worked_example(session, "rising_star")
        assert example is not None
        assert example.independent_multiplier_applied is True
        assert example.independent_flag_value == 1.0


def test_hidden_gem_example_has_low_mentions(test_engine):
    """Under hidden_gem, the top maker must have ≤ cap mentions
    (cap is max_mentions_for_eligibility = 2)."""
    _seed_diverse_population()

    with session_scope() as session:
        example = build_worked_example(session, "hidden_gem")
        assert example is not None
        assert example.cap_value == 2
        mention_row = next(
            r for r in example.rows if r.signal_name == "mention_count"
        )
        assert mention_row.raw_value <= 2


# ---------- Structure tests ----------


def test_worked_example_includes_all_signals(test_engine):
    """All 7 signals should appear in the rows, even ones with weight 0."""
    _seed_diverse_population()

    with session_scope() as session:
        example = build_worked_example(session, "rising_star")
        assert example is not None
        row_names = {r.signal_name for r in example.rows}
        assert row_names == set(SIGNAL_DESCRIPTIONS.keys())


def test_independent_flag_has_no_zscore(test_engine):
    """The independent_flag row must have z_value=None — it's a pass-through
    binary, not a z-scored continuous signal."""
    _seed_diverse_population()

    with session_scope() as session:
        example = build_worked_example(session, "rising_star")
        assert example is not None
        flag_row = next(
            r for r in example.rows if r.signal_name == "independent_flag"
        )
        assert flag_row.z_value is None
        # But the other rows should have z-scores
        other_rows = [
            r for r in example.rows if r.signal_name != "independent_flag"
        ]
        for r in other_rows:
            assert r.z_value is not None, (
                f"{r.signal_name} has no z-score but should be continuous"
            )


def test_contributions_sum_to_weighted_sum(test_engine):
    """The individual contributions should sum to the weighted_sum field.
    This is the math check — if it doesn't, the template would show rows
    that don't add up."""
    _seed_diverse_population()

    with session_scope() as session:
        example = build_worked_example(session, "rising_star")
        assert example is not None
        total = sum(r.contribution for r in example.rows)
        assert abs(total - example.weighted_sum) < 1e-9


# ---------- Empty-state test ----------


def test_build_worked_example_returns_none_when_no_signals(test_engine):
    """If no scans have been run, there's nothing to build an example from."""
    # Empty DB — no makers, no signals.
    with session_scope() as session:
        example = build_worked_example(session, "rising_star")
        assert example is None


# ---------- Route-level tests ----------


def test_methodology_route_renders(client):
    """GET /methodology returns 200 and includes expected content."""
    _seed_diverse_population()
    r = client.get("/methodology")
    assert r.status_code == 200
    # Should include all signal labels from SIGNAL_DESCRIPTIONS
    for meta in SIGNAL_DESCRIPTIONS.values():
        assert meta["label"] in r.text
    # Should include both preset names (title-cased)
    assert "Rising Star" in r.text
    assert "Hidden Gem" in r.text
    # Should include a worked example (contribution column)
    assert "Contribution" in r.text
    # Should include the z-score explanation
    assert "z-score" in r.text.lower() or "z = " in r.text


def test_methodology_route_works_with_empty_db(client):
    """If no scans have run, the page should still render (no crash) but
    show a 'no data yet' state instead of worked examples."""
    r = client.get("/methodology")
    assert r.status_code == 200
    # Static sections still render
    assert "Rising Star" in r.text  # preset listing
    # But worked examples are absent or show the empty message
    assert "No scored data yet" in r.text


def test_methodology_link_appears_on_detail_page(client):
    """The 'How is this scored?' link should appear on the maker detail
    page next to the signal breakdown."""
    mids = _seed_diverse_population()
    indie_id = mids["Popular Indie"]

    r = client.get(f"/makers/{indie_id}")
    assert r.status_code == 200
    assert 'href="/methodology"' in r.text
    assert "How is this scored?" in r.text


def test_methodology_link_appears_in_header(client):
    """The top-nav methodology link should appear on every page."""
    r = client.get("/")
    assert r.status_code == 200
    # It's in the header, so it appears on the dashboard too.
    assert 'href="/methodology"' in r.text


# ---------- Source contributions tests (added 2H) ----------


def _seed_source_audit_population():
    """Build a population designed to exercise each column of the source
    contributions table with non-trivial values.

    Sources:
      - SJX (media, 1.0): mentions 3 makers; first to mention 1
      - Monochrome (media, 0.9): mentions 2 makers; first to mention 1
      - GPHG 2025 (awards, 1.2): mentions 4 makers; first to mention 2
      - QuietSource (media, 0.5): no mentions (stale source case)
    """
    with session_scope() as session:
        sjx = SourceRow(name="SJX", category="media", prestige_weight=1.0)
        mono = SourceRow(name="Monochrome", category="media", prestige_weight=0.9)
        gphg = SourceRow(name="GPHG 2025", category="awards", prestige_weight=1.2)
        quiet = SourceRow(name="QuietSource", category="media", prestige_weight=0.5)
        session.add_all([sjx, mono, gphg, quiet])
        session.flush()

        a = Maker(canonical_name="Maker A", is_independent=True)
        b = Maker(canonical_name="Maker B", is_independent=True)
        c = Maker(canonical_name="Maker C", is_independent=True)
        d = Maker(canonical_name="Maker D", is_independent=True)
        session.add_all([a, b, c, d])
        session.flush()

        now = datetime.now(timezone.utc)
        old = now - timedelta(days=30)
        recent = now - timedelta(days=2)

        # Maker A: first seen by GPHG (old), later picked up by SJX (recent)
        session.add_all([
            Mention(maker_id=a.id, source_id=gphg.id, url="g/a", title="x",
                    discovered_at=old, published_at=old),
            Mention(maker_id=a.id, source_id=sjx.id, url="s/a", title="x",
                    discovered_at=recent, published_at=recent),
        ])
        # Maker B: first seen by GPHG, only by GPHG
        session.add(Mention(maker_id=b.id, source_id=gphg.id, url="g/b", title="x",
                            discovered_at=old, published_at=old))
        # Maker C: first seen by SJX (recent), also Monochrome (recent)
        session.add_all([
            Mention(maker_id=c.id, source_id=sjx.id, url="s/c", title="x",
                    discovered_at=recent, published_at=recent),
            Mention(maker_id=c.id, source_id=mono.id, url="m/c", title="x",
                    discovered_at=recent, published_at=recent),
        ])
        # Maker D: first seen by Monochrome only (recent)
        session.add(Mention(maker_id=d.id, source_id=mono.id, url="m/d", title="x",
                            discovered_at=recent, published_at=recent))
        # GPHG also has another mention of A (already counted above)
        # Bonus: SJX also mentions A again (recent) — adds to total but not distinct
        session.add(Mention(maker_id=a.id, source_id=sjx.id, url="s/a-2", title="y",
                            discovered_at=recent, published_at=recent))


def test_source_contributions_returns_one_row_per_source(test_engine):
    """Every configured source — even one with zero mentions — appears in
    the table. Sources without mentions are visible so calibration drift
    (a feed gone silent) is detectable."""
    _seed_source_audit_population()
    with session_scope() as session:
        rows = build_source_contributions(session)

    names = {r.source_name for r in rows}
    assert names == {"SJX", "Monochrome", "GPHG 2025", "QuietSource"}

    quiet = next(r for r in rows if r.source_name == "QuietSource")
    assert quiet.total_mentions == 0
    assert quiet.distinct_makers == 0
    assert quiet.first_surfaced_count == 0


def test_source_contributions_total_mentions_correct(test_engine):
    """Total mentions count every Mention row, including duplicates from
    the same maker (since each is an independent article/article-mention)."""
    _seed_source_audit_population()
    with session_scope() as session:
        rows = build_source_contributions(session)
    by_name = {r.source_name: r for r in rows}

    # SJX has 3 mentions: A (×2, once each from URLs s/a and s/a-2) + C
    assert by_name["SJX"].total_mentions == 3
    # Monochrome has 2 mentions: C, D
    assert by_name["Monochrome"].total_mentions == 2
    # GPHG has 2 mentions: A, B
    assert by_name["GPHG 2025"].total_mentions == 2


def test_source_contributions_distinct_makers_correct(test_engine):
    """Distinct makers counts unique maker_id, not raw mention rows."""
    _seed_source_audit_population()
    with session_scope() as session:
        rows = build_source_contributions(session)
    by_name = {r.source_name: r for r in rows}

    # SJX mentions A (twice) and C → 2 distinct
    assert by_name["SJX"].distinct_makers == 2
    # Monochrome mentions C and D → 2 distinct
    assert by_name["Monochrome"].distinct_makers == 2
    # GPHG mentions A and B → 2 distinct
    assert by_name["GPHG 2025"].distinct_makers == 2


def test_source_contributions_first_surfaced_correct(test_engine):
    """First-surfaced is the count of makers whose earliest mention came
    from this source. The discovery-quality signal."""
    _seed_source_audit_population()
    with session_scope() as session:
        rows = build_source_contributions(session)
    by_name = {r.source_name: r for r in rows}

    # GPHG was first for A and B (both have GPHG mentions at `old` timestamp,
    # earlier than any SJX/Monochrome timestamp) → 2
    assert by_name["GPHG 2025"].first_surfaced_count == 2
    # SJX was first for C (both SJX and Monochrome saw C at the same `recent`
    # timestamp; ties are resolved by min() semantics — the test setup
    # added SJX first, so SJX gets the credit. Either resolution is
    # defensible; we just need it to be deterministic and total to 4.)
    # Monochrome was first for D
    assert by_name["Monochrome"].first_surfaced_count == 1
    # Total first-surfaced across all sources should equal total makers
    total_surfaced = sum(r.first_surfaced_count for r in rows)
    assert total_surfaced == 4  # 4 makers total


def test_source_contributions_recent_window(test_engine):
    """Last-7-days mentions count only mentions discovered within the
    recent window."""
    _seed_source_audit_population()
    with session_scope() as session:
        rows = build_source_contributions(session)
    by_name = {r.source_name: r for r in rows}

    # SJX: 2 recent (s/a recent, s/c recent, s/a-2 recent) = 3 recent
    assert by_name["SJX"].mentions_recent == 3
    # Monochrome: m/c, m/d = 2 recent
    assert by_name["Monochrome"].mentions_recent == 2
    # GPHG: g/a and g/b are 30 days old, both outside 7-day window
    assert by_name["GPHG 2025"].mentions_recent == 0


def test_source_contributions_sorted_by_total_mentions(test_engine):
    """Rows are returned sorted by total_mentions descending. Makes the
    most active sources appear first in the table."""
    _seed_source_audit_population()
    with session_scope() as session:
        rows = build_source_contributions(session)

    totals = [r.total_mentions for r in rows]
    assert totals == sorted(totals, reverse=True)


def test_source_contributions_empty_db(test_engine):
    """With no sources, the function returns an empty list (not None)."""
    with session_scope() as session:
        rows = build_source_contributions(session)
    assert rows == []


def test_source_contributions_appears_on_methodology_page(client):
    """The /methodology page should render the new section with real data."""
    _seed_source_audit_population()
    r = client.get("/methodology")
    assert r.status_code == 200
    assert "Source Contributions" in r.text
    assert "First-surfaced" in r.text
    # Source names appear in the rendered table
    assert "SJX" in r.text
    assert "Monochrome" in r.text
    assert "GPHG 2025" in r.text


def test_methodology_page_handles_zero_mention_sources(client):
    """A configured source with zero mentions should still render on the
    page — important for spotting feeds that have gone silent."""
    _seed_source_audit_population()
    r = client.get("/methodology")
    assert r.status_code == 200
    assert "QuietSource" in r.text
