"""Tests for resolver affix matching and maker maintenance ops (chunk 3N)."""
from __future__ import annotations

import pytest

from watch_app.extraction.maker_admin import (
    find_near_duplicates,
    merge_makers,
    remove_alias,
)
from watch_app.extraction.resolver import resolve_or_create_maker
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Source


# ---------- affix-aware resolution (end-to-end) ----------


def test_resolver_matches_across_affix(test_engine):
    with session_scope() as s:
        seed = Maker(canonical_name="Atelier Akrivia")
        s.add(seed)
        s.flush()
        sid = seed.id
        # "Akrivia" should resolve to the existing maker, not create a new row.
        assert resolve_or_create_maker(s, "Akrivia").id == sid


def test_resolver_folds_initials(test_engine):
    with session_scope() as s:
        seed = Maker(canonical_name="F.P. Journe")
        s.add(seed)
        s.flush()
        sid = seed.id
        assert resolve_or_create_maker(s, "FP Journe").id == sid


def test_affix_does_not_merge_distinct_co_makers(test_engine):
    # "& Co." is a generic affix, stripped for matching — but the remaining
    # names differ ("masa" vs "jacob"), so two different "& Co." makers stay
    # separate rather than fuzzy-clustering on the shared suffix.
    with session_scope() as s:
        s.add(Maker(canonical_name="Masa & Co."))
        s.flush()
        m = resolve_or_create_maker(s, "Jacob & Co.")
        assert m.canonical_name == "Jacob & Co."  # new row, not merged into Masa
        assert s.query(Maker).count() == 2


# ---------- merge ----------


def test_merge_reassigns_dedups_and_deletes(test_engine):
    with session_scope() as s:
        a = Maker(canonical_name="Zzz From", aliases=["FromAlias"])
        b = Maker(canonical_name="Zzz Into", aliases=[])
        src = Source(name="SrcA", category="media")
        s.add_all([a, b, src])
        s.flush()
        s.add(Mention(source_id=src.id, maker_id=a.id, url="http://x/1", title="t1"))
        s.add(Mention(source_id=src.id, maker_id=a.id, url="http://x/2", title="t2"))
        # B already has a mention with the same (source, url) as A's /1 → dup.
        s.add(Mention(source_id=src.id, maker_id=b.id, url="http://x/1", title="t1b"))
        aid, bid = a.id, b.id

    with session_scope() as s:
        plan = merge_makers(s, str(aid), str(bid), commit=True)

    assert plan.done
    assert plan.mentions_reassigned == 1   # /2
    assert plan.mentions_deduped == 1      # /1 collides on target
    assert "Zzz From" in plan.aliases_added

    with session_scope() as s:
        assert s.query(Maker).filter_by(id=aid).first() is None
        b2 = s.query(Maker).filter_by(id=bid).first()
        assert "Zzz From" in b2.aliases and "FromAlias" in b2.aliases
        assert s.query(Mention).filter_by(maker_id=bid).count() == 2


def test_merge_dry_run_changes_nothing(test_engine):
    with session_scope() as s:
        a = Maker(canonical_name="Zzz DryFrom")
        b = Maker(canonical_name="Zzz DryInto")
        s.add_all([a, b])
        s.flush()
        aid, bid = a.id, b.id
    with session_scope() as s:
        plan = merge_makers(s, str(aid), str(bid), commit=False)
    assert not plan.done
    with session_scope() as s:
        assert s.query(Maker).filter_by(id=aid).first() is not None


def test_merge_from_seed_blocked(test_engine):
    # "Philippe Dufour" is a real seed maker.
    with session_scope() as s:
        a = Maker(canonical_name="Philippe Dufour")
        b = Maker(canonical_name="Zzz Target")
        s.add_all([a, b])
        s.flush()
        aid, bid = a.id, b.id
    with session_scope() as s:
        plan = merge_makers(s, str(aid), str(bid))
    assert plan.blockers and "seed maker" in plan.blockers[0]


def test_merge_self_blocked(test_engine):
    with session_scope() as s:
        a = Maker(canonical_name="Zzz Solo")
        s.add(a)
        s.flush()
        aid = a.id
    with session_scope() as s:
        plan = merge_makers(s, str(aid), str(aid))
    assert any("itself" in b for b in plan.blockers)


def test_merge_missing_blocked(test_engine):
    with session_scope() as s:
        plan = merge_makers(s, "999998", "999999")
    assert len(plan.blockers) == 2


# ---------- unalias ----------


def test_unalias_removes(test_engine):
    with session_scope() as s:
        m = Maker(canonical_name="Zzz Owner", aliases=["Good", "Bad"])
        s.add(m)
        s.flush()
        mid = m.id
    with session_scope() as s:
        res = remove_alias(s, str(mid), "Bad", commit=True)
    assert res.removed
    with session_scope() as s:
        assert s.query(Maker).filter_by(id=mid).first().aliases == ["Good"]


def test_unalias_dry_run(test_engine):
    with session_scope() as s:
        m = Maker(canonical_name="Zzz Owner2", aliases=["Keep", "Drop"])
        s.add(m)
        s.flush()
        mid = m.id
    with session_scope() as s:
        res = remove_alias(s, str(mid), "Drop", commit=False)
    assert not res.removed
    with session_scope() as s:
        assert "Drop" in s.query(Maker).filter_by(id=mid).first().aliases


def test_unalias_not_an_alias_blocked(test_engine):
    with session_scope() as s:
        m = Maker(canonical_name="Zzz Owner3", aliases=["X"])
        s.add(m)
        s.flush()
        mid = m.id
    with session_scope() as s:
        res = remove_alias(s, str(mid), "Nope", commit=True)
    assert res.blockers and not res.removed


def test_unalias_seed_warning(test_engine):
    # A maker named like a seed entry triggers the re-add warning.
    with session_scope() as s:
        m = Maker(canonical_name="Philippe Dufour", aliases=["Dufour"])
        s.add(m)
        s.flush()
        mid = m.id
    with session_scope() as s:
        res = remove_alias(s, str(mid), "Dufour", commit=False)
    assert res.seed_warning


# ---------- dupes ----------


def test_find_near_duplicates(test_engine):
    with session_scope() as s:
        s.add_all(
            [
                Maker(canonical_name="Zzz Chronograph One"),
                Maker(canonical_name="Zzz Chronograph Onee"),
                Maker(canonical_name="Wildly Unrelated Brand"),
            ]
        )
        s.flush()
    with session_scope() as s:
        pairs = find_near_duplicates(s, threshold=85)
    names = {tuple(sorted([p.a_name, p.b_name])) for p in pairs}
    assert ("Zzz Chronograph One", "Zzz Chronograph Onee") in names
    assert all("Wildly Unrelated Brand" not in pair for pair in names)
