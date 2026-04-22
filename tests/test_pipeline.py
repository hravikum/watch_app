"""End-to-end test of the pipeline with the dummy source."""
from __future__ import annotations

from watch_app.pipeline.orchestrator import run_scan
from watch_app.sources.dummy import DummySource
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score


def test_scan_populates_db(test_engine):
    stats = run_scan(sources=[DummySource()], use_llm=False)

    assert stats["sources_run"] == 1
    assert stats["mentions_written"] > 0

    with session_scope() as session:
        maker_names = {m.canonical_name for m in session.query(Maker).all()}
        assert "Simon Brette" in maker_names
        assert "Raúl Pagès" in maker_names
        assert "Daizoh Makihara" in maker_names


def test_scan_is_idempotent(test_engine):
    """Running the same scan twice shouldn't double-count mentions."""
    run_scan(sources=[DummySource()], use_llm=False)
    with session_scope() as session:
        first_count = session.query(Mention).count()

    run_scan(sources=[DummySource()], use_llm=False)
    with session_scope() as session:
        second_count = session.query(Mention).count()

    assert first_count == second_count


def test_scoring_assigns_ranks(test_engine):
    run_scan(sources=[DummySource()], use_llm=False)

    with session_scope() as session:
        # Simon Brette has 2 mentions in the dummy data, others have 1.
        # Under rising_star (weight=1.0 on mention_count), Simon should be #1.
        latest_rising = (
            session.query(Score)
            .filter(Score.preset == "rising_star")
            .order_by(Score.rank.asc())
            .all()
        )
        assert latest_rising[0].rank == 1
        top_maker = session.get(Maker, latest_rising[0].maker_id)
        assert top_maker.canonical_name == "Simon Brette"
