"""Pytest fixtures.

Each test gets a fresh in-memory SQLite database. The global engine in
watch_app.storage.db is swapped out so code under test uses our fixture DB.

Uses StaticPool so the single in-memory DB is shared across all connections
— important for the web tests, where FastAPI handlers open their own
sessions via session_scope().
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from watch_app.storage import db as db_module
from watch_app.storage.models import Base


@pytest.fixture
def test_engine():
    """A fresh in-memory SQLite engine with all tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)

    # Swap into the global slot so production code uses this engine.
    original_engine = db_module._engine
    original_factory = db_module._SessionLocal
    db_module._engine = engine
    db_module._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    yield engine

    # Restore.
    db_module._engine = original_engine
    db_module._SessionLocal = original_factory
    engine.dispose()


@pytest.fixture
def session(test_engine) -> Session:
    """A session bound to the test engine. Auto-rolled-back at teardown."""
    SessionLocal = sessionmaker(bind=test_engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _stub_llm_enrichment(monkeypatch):
    """Keep tests from making real Anthropic / website calls during a scan.

    The orchestrator's enrichment and taste phases run on every ``run_scan``
    (they're independent of ``use_llm``, which only governs entity
    extraction). Without this, any test that calls ``run_scan`` issues real,
    billable LLM lookups for every unenriched seed maker plus real
    website-verification HTTP — which is what made the orchestrator tests take
    20-30s each and the full suite many minutes (and cost money) on a machine
    with network and a valid key.

    We stub the four enrichment/taste lookups to abstain by default. Tests that
    exercise enrichment or taste directly patch these same names inside their
    own ``with patch(...)`` blocks, which override this default for their scope.
    """
    from watch_app.extraction import enrich

    monkeypatch.setattr(enrich, "lookup_maker_website", lambda *a, **k: None)
    monkeypatch.setattr(enrich, "lookup_maker_country", lambda *a, **k: None)
    monkeypatch.setattr(enrich, "lookup_maker_independence", lambda *a, **k: None)
    monkeypatch.setattr(enrich, "lookup_maker_taste", lambda *a, **k: {})


@pytest.fixture(autouse=True)
def _block_real_http(request, monkeypatch):
    """Fail fast if a test triggers a real network fetch via watch_app.http.get.

    Each source already mocks its own ``http_get``, but a full ``run_scan``
    also reaches ``watch_app.http.get`` from the GPHG detail-fetch step
    (``_fetch_gphg_models``). Left unmocked that hits the network, and the
    1.5s/host rate limiter serializes the calls — ~6s per scan test on its own.

    Tests that exercise the real client are marked ``real_http`` (its httpx
    layer is still mocked there). Tests that drive ``_fetch_gphg_models``
    directly patch ``watch_app.http.get`` themselves, which overrides this.
    """
    if request.node.get_closest_marker("real_http"):
        return
    import watch_app.http as http_module

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "Real HTTP blocked in tests — patch watch_app.http.get (or the "
            "source's http_get) with a fixture response."
        )

    monkeypatch.setattr(http_module, "get", _blocked)
