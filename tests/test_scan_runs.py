"""Tests for the ScanRun lifecycle — creation, completion, failure handling.

These tests exercise run_scan() end-to-end and verify that scan history is
recorded correctly, including on failure. Uses DummySource to avoid network
calls.
"""
from __future__ import annotations

import pytest

from watch_app.pipeline.orchestrator import run_scan
from watch_app.sources.base import RawMention, Source
from watch_app.sources.dummy import DummySource
from watch_app.storage.db import session_scope
from watch_app.storage.models import ScanRun


class _BrokenSource(Source):
    """A source whose fetch() raises — used to test failure handling."""

    name = "Broken"
    category = "media"
    prestige_weight = 0.5

    def fetch(self):
        raise RuntimeError("simulated network failure")


class _CrashOnLastSource(Source):
    """Source that raises only after yielding one mention.

    A single source raising is caught and skipped by the orchestrator, which
    is the behavior we want (one bad feed shouldn't kill the scan). This
    source exercises that path: the ScanRun should still complete.
    """

    name = "PartialCrash"
    category = "media"
    prestige_weight = 0.5

    def fetch(self):
        yield RawMention(
            source_name=self.name,
            url="https://example/1",
            title="one mention",
            candidate_names=["Simon Brette"],
        )
        raise RuntimeError("after one mention")


# ---------- Happy path ----------


def test_successful_scan_records_completed_run(test_engine):
    """A clean run should produce one ScanRun with status='completed' and counts."""
    stats = run_scan(sources=[DummySource()], use_llm=False)

    assert "scan_run_id" in stats
    run_id = stats["scan_run_id"]

    with session_scope() as session:
        run = session.get(ScanRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.finished_at is not None
        assert run.started_at is not None
        assert run.finished_at >= run.started_at
        assert run.sources_run == 1
        assert run.mentions_written >= 0
        assert run.makers_scored is not None
        assert run.trigger == "manual"
        assert run.error_message is None


def test_trigger_is_recorded(test_engine):
    """`trigger` param should land in the ScanRun row."""
    stats = run_scan(sources=[DummySource()], use_llm=False, trigger="scheduled")

    with session_scope() as session:
        run = session.get(ScanRun, stats["scan_run_id"])
        assert run.trigger == "scheduled"


def test_multiple_scans_produce_multiple_runs(test_engine):
    """Each invocation of run_scan creates a new row."""
    stats1 = run_scan(sources=[DummySource()], use_llm=False)
    stats2 = run_scan(sources=[DummySource()], use_llm=False)

    assert stats1["scan_run_id"] != stats2["scan_run_id"]

    with session_scope() as session:
        count = session.query(ScanRun).count()
        assert count == 2


# ---------- Per-source failure (graceful) ----------


def test_single_broken_source_does_not_fail_whole_scan(test_engine):
    """Orchestrator catches per-source exceptions and continues; status should
    still be 'completed' because the scan as a whole ran."""
    stats = run_scan(
        sources=[DummySource(), _BrokenSource()],
        use_llm=False,
    )

    with session_scope() as session:
        run = session.get(ScanRun, stats["scan_run_id"])
        assert run.status == "completed"
        # DummySource ran, Broken didn't, so sources_run = 1
        assert run.sources_run == 1


# ---------- Scan-level failure ----------


def test_unexpected_failure_records_failed_status(test_engine, monkeypatch):
    """If something raises OUTSIDE the per-source try/except (e.g. during
    ranker), the ScanRun should be marked 'failed' with the error captured,
    and the exception should propagate up."""
    from watch_app.pipeline import orchestrator as orch_module

    def exploding_ranker(*args, **kwargs):
        raise ValueError("ranker exploded for testing")

    # The orchestrator binds rank_all_makers at import time, so we patch
    # in the orchestrator's namespace, not the ranker module's.
    monkeypatch.setattr(orch_module, "rank_all_makers", exploding_ranker)

    with pytest.raises(ValueError, match="ranker exploded"):
        run_scan(sources=[DummySource()], use_llm=False)

    # The ScanRun row should exist and be marked failed, even though the
    # exception propagated. This is the core guarantee.
    with session_scope() as session:
        runs = (
            session.query(ScanRun)
            .order_by(ScanRun.started_at.desc())
            .limit(1)
            .all()
        )
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].finished_at is not None
        assert runs[0].error_message is not None
        assert "ValueError" in runs[0].error_message
        assert "ranker exploded" in runs[0].error_message


def test_duration_property(test_engine):
    """Smoke test: duration_seconds computes correctly on completed runs."""
    stats = run_scan(sources=[DummySource()], use_llm=False)

    with session_scope() as session:
        run = session.get(ScanRun, stats["scan_run_id"])
        dur = run.duration_seconds
        assert dur is not None
        assert dur >= 0.0
        assert dur < 60.0  # Sanity — dummy scan shouldn't take a minute
