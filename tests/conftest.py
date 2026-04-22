"""Pytest fixtures.

Each test gets a fresh in-memory SQLite database. The global engine in
watch_app.storage.db is swapped out so code under test uses our fixture DB.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from watch_app.storage import db as db_module
from watch_app.storage.models import Base


@pytest.fixture
def test_engine():
    """A fresh in-memory SQLite engine with all tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
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
