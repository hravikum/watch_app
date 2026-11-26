"""Tests for the delete-tombstone blocklist (chunk 4K).

Hard delete used to leave nothing behind, so the resolver's create step
resurrected the maker as soon as any live article mentioned the name again.
Now delete_maker writes a blocked_makers tombstone (canonical + aliases) on
commit, and resolve_or_create_maker refuses to CREATE any candidate matching
it — same normalize + fuzzy >=90 semantics as matching. Existing-maker
matching is never gated, and unblock_maker reverses the tombstone.
"""
from __future__ import annotations

from watch_app.extraction.maker_admin import delete_maker, list_blocked, unblock_maker
from watch_app.extraction.resolver import resolve_or_create_maker
from watch_app.storage.db import session_scope
from watch_app.storage.models import BlockedMaker, Maker


def _make_maker(s, name: str, aliases: list[str] | None = None) -> int:
    m = Maker(canonical_name=name, aliases=aliases or [])
    s.add(m)
    s.flush()
    return m.id


# ---------- delete writes the tombstone ----------


def test_delete_commit_writes_tombstone_with_aliases(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand", aliases=["Zzz Junk Brand"])
        plan = delete_maker(s, "Zzz Junkbrand", commit=True)
        assert plan.done is True
        assert plan.names_blocked == ["Zzz Junkbrand", "Zzz Junk Brand"]

        row = s.query(BlockedMaker).filter_by(canonical_name="Zzz Junkbrand").one()
        assert row.aliases == ["Zzz Junk Brand"]
        assert row.blocked_at is not None
        assert s.query(Maker).filter_by(canonical_name="Zzz Junkbrand").count() == 0


def test_delete_dry_run_writes_no_tombstone(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand")
        plan = delete_maker(s, "Zzz Junkbrand", commit=False)
        assert plan.done is False
        assert plan.names_blocked == ["Zzz Junkbrand"]  # previewed, not written
        assert s.query(BlockedMaker).count() == 0
        assert s.query(Maker).filter_by(canonical_name="Zzz Junkbrand").count() == 1


def test_redelete_updates_existing_tombstone_and_unions_aliases(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand", aliases=["Zzz JB"])
        delete_maker(s, "Zzz Junkbrand", commit=True)
        # Unblock, re-discover under the same name with a NEW alias, re-delete.
        unblock_maker(s, "Zzz Junkbrand", commit=True)
        _make_maker(s, "Zzz Junkbrand", aliases=["Zzz Junk Co"])
        delete_maker(s, "Zzz Junkbrand", commit=True)

        rows = s.query(BlockedMaker).filter_by(canonical_name="Zzz Junkbrand").all()
        assert len(rows) == 1  # unique canonical — updated, not duplicated
        assert rows[0].aliases == ["Zzz Junk Co"]


def test_redelete_without_unblock_unions_aliases(test_engine):
    # Direct re-delete path (tombstone still present, maker recreated manually):
    # the existing tombstone's aliases are unioned, not replaced.
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand", aliases=["Zzz JB"])
        delete_maker(s, "Zzz Junkbrand", commit=True)
        s.add(Maker(canonical_name="Zzz Junkbrand", aliases=["Zzz Junk Co"]))
        s.flush()
        delete_maker(s, "Zzz Junkbrand", commit=True)

        row = s.query(BlockedMaker).filter_by(canonical_name="Zzz Junkbrand").one()
        assert row.aliases == ["Zzz JB", "Zzz Junk Co"]


# ---------- resolver gate ----------


def test_resolver_blocks_exact_name(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand")
        delete_maker(s, "Zzz Junkbrand", commit=True)
        assert resolve_or_create_maker(s, "Zzz Junkbrand") is None
        assert s.query(Maker).filter_by(canonical_name="Zzz Junkbrand").count() == 0


def test_resolver_blocks_blocked_alias(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand", aliases=["Zzz Junk Brand"])
        delete_maker(s, "Zzz Junkbrand", commit=True)
        assert resolve_or_create_maker(s, "Zzz Junk Brand") is None


def test_resolver_blocks_fuzzy_variant(test_engine):
    # A variant spelling that would have fuzzy-resolved (>=90) into the deleted
    # maker is blocked too — same semantics as matching, so a typo in a feed
    # can't resurrect the maker under a near-name.
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand Watches")
        delete_maker(s, "Zzz Junkbrand Watches", commit=True)
        # Affix-stripped ("Watches") + exact-normalized remainder.
        assert resolve_or_create_maker(s, "Zzz Junkbrand") is None
        # Fuzzy near-miss spelling.
        assert resolve_or_create_maker(s, "Zzz Junkbrand Watchs") is None


def test_resolver_still_creates_unrelated_names(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand")
        delete_maker(s, "Zzz Junkbrand", commit=True)
        m = resolve_or_create_maker(s, "Daizoh Makihara")
        assert m is not None
        assert m.canonical_name == "Daizoh Makihara"


def test_blocklist_never_gates_existing_maker_match(test_engine):
    # The gate sits only at the create step: if a maker EXISTS whose name
    # matches a tombstone (e.g. re-added deliberately via the seed YAML, which
    # bypasses the resolver), candidates still resolve to it.
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand")
        delete_maker(s, "Zzz Junkbrand", commit=True)
        s.add(Maker(canonical_name="Zzz Junkbrand"))  # seed-loader-style re-add
        s.flush()
        m = resolve_or_create_maker(s, "Zzz Junkbrand")
        assert m is not None
        assert m.canonical_name == "Zzz Junkbrand"


# ---------- orchestrator ingest skips blocked candidates ----------


def test_ingest_mention_skips_blocked_candidate(test_engine):
    from watch_app.pipeline.orchestrator import _ingest_mention
    from watch_app.sources.base import RawMention
    from watch_app.storage.models import Mention, Source

    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand")
        delete_maker(s, "Zzz Junkbrand", commit=True)
        src = Source(name="SrcBlock", category="media")
        s.add(src)
        s.flush()

        raw = RawMention(
            source_name="SrcBlock",
            url="http://x/blocked-article",
            title="Not a debut title",
            candidate_names=["Zzz Junkbrand", "Daizoh Makihara"],
        )
        written = _ingest_mention(s, raw, src, use_llm=False)

        # Blocked candidate dropped; the other one written normally.
        assert written == 1
        assert s.query(Maker).filter_by(canonical_name="Zzz Junkbrand").count() == 0
        kept = s.query(Maker).filter_by(canonical_name="Daizoh Makihara").one()
        assert s.query(Mention).filter_by(maker_id=kept.id).count() == 1


# ---------- unblock ----------


def test_unblock_removes_tombstone_and_allows_recreation(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand", aliases=["Zzz JB"])
        delete_maker(s, "Zzz Junkbrand", commit=True)

        res = unblock_maker(s, "Zzz Junkbrand", commit=True)
        assert res.done is True
        assert res.aliases_freed == ["Zzz JB"]
        assert s.query(BlockedMaker).count() == 0

        m = resolve_or_create_maker(s, "Zzz Junkbrand")
        assert m is not None


def test_unblock_dry_run_keeps_tombstone(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand")
        delete_maker(s, "Zzz Junkbrand", commit=True)
        res = unblock_maker(s, "Zzz Junkbrand", commit=False)
        assert res.done is False
        assert s.query(BlockedMaker).count() == 1


def test_unblock_by_id_and_normalized_name(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Junkbrand")
        delete_maker(s, "Zzz Junkbrand", commit=True)
        row = s.query(BlockedMaker).one()
        # By id.
        assert unblock_maker(s, str(row.id), commit=False).blocked_name == "Zzz Junkbrand"
        # By normalized-name fallback (case-fold).
        assert (
            unblock_maker(s, "zzz junkbrand", commit=False).blocked_name
            == "Zzz Junkbrand"
        )


def test_unblock_unknown_name_reports_blocker(test_engine):
    with session_scope() as s:
        res = unblock_maker(s, "Nope Nothing", commit=True)
        assert res.blockers
        assert res.done is False


def test_list_blocked_orders_by_name(test_engine):
    with session_scope() as s:
        _make_maker(s, "Zzz Bbb")
        _make_maker(s, "Zzz Aaa")
        delete_maker(s, "Zzz Bbb", commit=True)
        delete_maker(s, "Zzz Aaa", commit=True)
        names = [b.canonical_name for b in list_blocked(s)]
        assert names == ["Zzz Aaa", "Zzz Bbb"]
