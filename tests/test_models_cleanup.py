"""Tests for the 2V-cleanup: dead Model class removal.

Verifies that deleting a Maker row via SQLAlchemy ORM doesn't trigger
a cascade-load against a nonexistent ``models`` table. This was the
latent bug that surfaced when cleanup_2u_clutter.py tried to delete
clutter Maker rows — SQLAlchemy's relationship loader followed the
phantom ``Maker.models`` relationship into a table that didn't exist.

After 2V-cleanup, ``Maker`` has a ``watch_models`` relationship pointing
at the real ``WatchModel`` class (table ``watch_models``). Deletes
work normally.
"""
from __future__ import annotations

from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, WatchModel


def test_can_delete_maker_via_orm(test_engine):
    """ORM-level delete of a Maker shouldn't blow up trying to load
    related ``models`` (the dead-class bug). With 2V-cleanup the
    relationship points at WatchModel and the delete succeeds."""
    with session_scope() as session:
        m = Maker(canonical_name="To Be Deleted", is_independent=True)
        session.add(m)
        session.flush()
        maker_id = m.id

    # Re-open session and delete via ORM. This is the operation that
    # previously failed with "no such table: models".
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
        session.delete(m)
        session.commit()

    with session_scope() as session:
        result = session.query(Maker).filter_by(id=maker_id).one_or_none()
    assert result is None


def test_maker_watch_models_relationship_works(test_engine):
    """The ``Maker.watch_models`` relationship can be accessed and
    returns a list (empty for a maker with no GPHG entries). This
    verifies the relationship is correctly wired to WatchModel."""
    with session_scope() as session:
        m = Maker(canonical_name="Has No Models", is_independent=True)
        session.add(m)
        session.flush()
        # Access the relationship — would raise if mis-wired.
        watch_models = list(m.watch_models)
    assert watch_models == []


def test_maker_watch_models_returns_watchmodel_rows(test_engine):
    """When WatchModel rows exist for a maker, the relationship
    returns them."""
    with session_scope() as session:
        m = Maker(canonical_name="Has Models", is_independent=True)
        session.add(m)
        session.flush()
        wm = WatchModel(
            maker_id=m.id,
            name="Test Model",
            slug="test-model",
            gphg_year=2025,
            gphg_url="https://example.com/test-model-2025",
        )
        session.add(wm)
        session.flush()
        maker_id = m.id

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
        names = [w.name for w in m.watch_models]
    assert names == ["Test Model"]
