"""SQLAlchemy engine and session factory.

Usage:
    from watch_app.storage.db import session_scope
    with session_scope() as session:
        session.add(...)
        # commit is automatic on successful exit, rollback on exception
"""
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from watch_app.config import get_settings


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Lazy-initialize and return the process-global engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        # check_same_thread=False is only needed for SQLite when used across
        # threads (e.g. FastAPI). Harmless otherwise.
        connect_args = {}
        if settings.db_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(settings.db_url, connect_args=connect_args, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Lazy-initialize and return the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager.

    Commits on success, rolls back on exception, always closes.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Reset the global engine and session factory.

    Only used by tests that swap DB URLs or by code that wants a fresh connection.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
