"""Tests for maker suppression (delete-vs-suppress, the soft path).

Covers:
  1. suppress_maker / unsuppress_maker behavior (dry-run vs commit, idempotence,
     unknown maker blocked)
  2. A suppressed maker drops out of fetch_discovered_makers (/discoveries)
  3. A suppressed maker drops out of fetch_top_makers (rankings)
  4. delete_maker removes a discovered maker and its dependents (the hard path)

These use the same fixtures the rest of the suite uses:
  - `test_engine` + session_scope()  (as in tests/test_discoveries.py, tests/test_maker_admin.py)
  - `session`                        (as in tests/test_queries.py)
If your conftest names them differently, adjust the fixture args below.
"""
from __future__ import annotations

from datetime import datetime, timezone

from watch_app.extraction.maker_admin import (
    delete_maker,
    list_suppressed,
    suppress_maker,
    unsuppress_maker,
)
from watch_app.scoring.discovery import fetch_discovered_makers
from watch_app.scoring.queries import fetch_top_makers
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score, Source


# A name that is NOT in the seed YAML, so it shows up in /discoveries.
DISCOVERED = "Definitely Not A Real Maker"


# ---------- suppress / unsuppress core ----------


def test_suppress_commit_sets_flag_and_reason(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name=DISCOVERED, is_independent=True))

    with session_scope() as s:
        res = suppress_maker(s, DISCOVERED, reason="defunct historical maker", commit=True)
    assert res.done and not res.blockers

    with session_scope() as s:
        m = s.query(Maker).filter_by(canonical_name=DISCOVERED).one()
        assert m.is_suppressed is True
        assert m.suppressed_reason == "defunct historical maker"


def test_suppress_dry_run_changes_nothing(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name=DISCOVERED, is_independent=True))

    with session_scope() as s:
        res = suppress_maker(s, DISCOVERED, reason="nope", commit=False)
    assert not res.done

    with session_scope() as s:
        m = s.query(Maker).filter_by(canonical_name=DISCOVERED).one()
        assert m.is_suppressed is False
        assert m.suppressed_reason is None


def test_suppress_is_idempotent(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name=DISCOVERED, is_independent=True))
    with session_scope() as s:
        suppress_maker(s, DISCOVERED, commit=True)
    with session_scope() as s:
        res = suppress_maker(s, DISCOVERED, commit=True)
    assert res.already_in_state and not res.done


def test_unsuppress_restores(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name=DISCOVERED, is_independent=True, is_suppressed=True,
                    suppressed_reason="x"))
    with session_scope() as s:
        res = unsuppress_maker(s, DISCOVERED, commit=True)
    assert res.done

    with session_scope() as s:
        m = s.query(Maker).filter_by(canonical_name=DISCOVERED).one()
        assert m.is_suppressed is False
        assert m.suppressed_reason is None


def test_suppress_unknown_maker_blocks(test_engine):
    with session_scope() as s:
        res = suppress_maker(s, "No Such Maker 9999", commit=True)
    assert res.blockers and not res.done


def test_list_suppressed_returns_only_suppressed(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name=DISCOVERED, is_independent=True, is_suppressed=True))
        s.add(Maker(canonical_name="Visible Maker", is_independent=True))
    with session_scope() as s:
        names = [m.canonical_name for m in list_suppressed(s)]
    assert DISCOVERED in names
    assert "Visible Maker" not in names


# ---------- visibility: discoveries ----------


def test_suppressed_excluded_from_discoveries(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name=DISCOVERED, is_independent=True))

    with session_scope() as s:
        assert DISCOVERED in [d.canonical_name for d in fetch_discovered_makers(s)]

    with session_scope() as s:
        suppress_maker(s, DISCOVERED, commit=True)

    with session_scope() as s:
        assert DISCOVERED not in [d.canonical_name for d in fetch_discovered_makers(s)]


def test_unsuppress_returns_to_discoveries(test_engine):
    with session_scope() as s:
        s.add(Maker(canonical_name=DISCOVERED, is_independent=True, is_suppressed=True))
    with session_scope() as s:
        assert DISCOVERED not in [d.canonical_name for d in fetch_discovered_makers(s)]
    with session_scope() as s:
        unsuppress_maker(s, DISCOVERED, commit=True)
    with session_scope() as s:
        assert DISCOVERED in [d.canonical_name for d in fetch_discovered_makers(s)]


# ---------- visibility: rankings ----------


def test_suppressed_excluded_from_top_makers(session):
    """A suppressed maker with a current score must not appear in fetch_top_makers."""
    now = datetime.now(timezone.utc)
    m = Maker(canonical_name=DISCOVERED, aliases=[], is_independent=True)
    session.add(m)
    session.flush()
    session.add(Score(maker_id=m.id, preset="rising_star", value=5.0, rank=1, computed_at=now))
    session.commit()

    names = [r["canonical_name"] for r in fetch_top_makers(session, "rising_star", limit=20)]
    assert DISCOVERED in names

    m.is_suppressed = True
    session.commit()

    names = [r["canonical_name"] for r in fetch_top_makers(session, "rising_star", limit=20)]
    assert DISCOVERED not in names


# ---------- the hard path: delete ----------


def test_delete_removes_maker_and_dependents(test_engine):
    with session_scope() as s:
        src = Source(name="t-src", category="media", url="http://x", prestige_weight=0.5)
        m = Maker(canonical_name="Zzz Doomed", is_independent=True)
        s.add_all([src, m])
        s.flush()
        s.add(Mention(source_id=src.id, maker_id=m.id, url="http://x/1", title="t"))
        mid = m.id

    with session_scope() as s:
        plan = delete_maker(s, str(mid), commit=True)
    assert plan.done and plan.mentions_deleted == 1

    with session_scope() as s:
        assert s.query(Maker).filter_by(id=mid).first() is None
        assert s.query(Mention).filter_by(maker_id=mid).count() == 0


def test_delete_dry_run_changes_nothing(test_engine):
    with session_scope() as s:
        m = Maker(canonical_name="Zzz DrySafe", is_independent=True)
        s.add(m)
        s.flush()
        mid = m.id
    with session_scope() as s:
        assert not delete_maker(s, str(mid), commit=False).done
    with session_scope() as s:
        assert s.query(Maker).filter_by(id=mid).first() is not None


def test_suppressed_excluded_from_review(test_engine):
    """A suppressed maker must not appear in the /review grid feed."""
    from watch_app.api.routers.makers import _fetch_discovered_for_review

    with session_scope() as s:
        s.add(Maker(canonical_name=DISCOVERED, is_independent=True))
    assert DISCOVERED in [r["canonical_name"] for r in _fetch_discovered_for_review()]

    with session_scope() as s:
        suppress_maker(s, DISCOVERED, commit=True)
    assert DISCOVERED not in [r["canonical_name"] for r in _fetch_discovered_for_review()]


def test_suppressed_excluded_from_comparison(session):
    """A suppressed maker with a score must not appear on /comparison."""
    from watch_app.scoring.queries import fetch_all_makers_comparison

    now = datetime.now(timezone.utc)
    m = Maker(canonical_name=DISCOVERED, aliases=[], is_independent=True)
    session.add(m)
    session.flush()
    session.add(Score(maker_id=m.id, preset="rising_star", value=5.0, rank=1, computed_at=now))
    session.commit()

    names = [r["canonical_name"] for r in fetch_all_makers_comparison(session, "rising_star", "hidden_gem")]
    assert DISCOVERED in names

    m.is_suppressed = True
    session.commit()

    names = [r["canonical_name"] for r in fetch_all_makers_comparison(session, "rising_star", "hidden_gem")]
    assert DISCOVERED not in names
