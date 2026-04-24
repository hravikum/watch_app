"""Tests for the methodology page's worked-example reconstruction.

Key invariant: the worked example's final score must match the ranker's
composite score for the same maker. These are computed from the same raw
signals via the same math, so if they drift apart, either the ranker or
the methodology helper is wrong.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.scoring.methodology import (
    SIGNAL_DESCRIPTIONS,
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
