"""Tests for taste tagging of auto-discovered makers (chunk 3F).

Verifies:
  1. Selection — only auto-discovered (non-seed) independents with
     taste_enriched_at IS NULL are in scope; seed makers, conglomerates,
     and already-attempted makers are excluded.
  2. auto_tag_taste_for_discovered writes sanitized tags, marks them in
     enriched_fields, and stamps taste_enriched_at.
  3. Abstention stamps taste_enriched_at but writes nothing (no retry churn).
  4. Out-of-vocabulary values from the LLM are dropped (sanitize).
  5. The never-overwrite rule holds for an already-set taste field.
  6. The per-scan cap is respected.
  7. Dry-run propose makes no writes.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from watch_app.extraction.enrich import (
    apply_taste_proposals,
    auto_tag_taste_for_discovered,
    iter_untagged_discovered_independents,
    propose_taste_for_discovered,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


# A name we can be confident is NOT in the seed YAML.
DISCOVERED_NAME = "Zzztest Discovery Brand"
# A name we know IS in the seed YAML (also used by tests/test_discoveries.py).
SEED_NAME = "Simon Brette"

VALID_TAGS = {
    "aesthetic_tags": ["modern", "avant-garde"],
    "complication_focus": ["tourbillon"],
    "maker_type": "solo-watchmaker",
    "price_band": "haute",
}


def _add_maker(**kwargs) -> int:
    """Insert a Maker and return its id."""
    with session_scope() as session:
        m = Maker(**kwargs)
        session.add(m)
        session.flush()
        return m.id


# ---------- Selection ----------


def test_discovered_independent_in_scope(test_engine):
    _add_maker(canonical_name=DISCOVERED_NAME, is_independent=True)
    with session_scope() as session:
        names = [m.canonical_name for m in iter_untagged_discovered_independents(session)]
    assert DISCOVERED_NAME in names


def test_seed_maker_out_of_scope(test_engine):
    """A maker whose name is in the seed YAML is not auto-tagged here —
    its taste is curated in the YAML."""
    _add_maker(canonical_name=SEED_NAME, is_independent=True)
    with session_scope() as session:
        names = [m.canonical_name for m in iter_untagged_discovered_independents(session)]
    assert SEED_NAME not in names


def test_conglomerate_out_of_scope(test_engine):
    _add_maker(canonical_name=DISCOVERED_NAME, is_independent=False)
    with session_scope() as session:
        names = [m.canonical_name for m in iter_untagged_discovered_independents(session)]
    assert DISCOVERED_NAME not in names


def test_already_attempted_out_of_scope(test_engine):
    """taste_enriched_at already set ⇒ not re-selected."""
    from datetime import datetime, timezone

    _add_maker(
        canonical_name=DISCOVERED_NAME,
        is_independent=True,
        taste_enriched_at=datetime.now(timezone.utc),
    )
    with session_scope() as session:
        names = [m.canonical_name for m in iter_untagged_discovered_independents(session)]
    assert DISCOVERED_NAME not in names


# ---------- auto_tag_taste_for_discovered ----------


def test_discovered_maker_gets_tagged(test_engine):
    _add_maker(canonical_name=DISCOVERED_NAME, is_independent=True, country="France")

    with patch(
        "watch_app.extraction.enrich.lookup_maker_taste", return_value=dict(VALID_TAGS)
    ):
        with session_scope() as session:
            written = auto_tag_taste_for_discovered(session, limit=25)

    assert written == 4  # two list fields + two single fields
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=DISCOVERED_NAME).one()
        assert m.aesthetic_tags == ["modern", "avant-garde"]
        assert m.complication_focus == ["tourbillon"]
        assert m.maker_type == "solo-watchmaker"
        assert m.price_band == "haute"
        assert m.taste_enriched_at is not None
        # The written fields are flagged "auto" for the review UI.
        assert set(m.enriched_fields) >= {
            "aesthetic_tags",
            "complication_focus",
            "maker_type",
            "price_band",
        }


def test_seed_maker_not_tagged_by_auto(test_engine):
    """End-to-end: a seed-named maker is left untouched by the auto path."""
    _add_maker(canonical_name=SEED_NAME, is_independent=True)
    mock = MagicMock(return_value=dict(VALID_TAGS))
    with patch("watch_app.extraction.enrich.lookup_maker_taste", mock):
        with session_scope() as session:
            written = auto_tag_taste_for_discovered(session, limit=25)
    assert written == 0
    mock.assert_not_called()
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=SEED_NAME).one()
        assert m.taste_enriched_at is None
        assert m.maker_type is None


def test_abstention_stamps_but_writes_nothing(test_engine):
    """A full abstention ({}) stamps taste_enriched_at so the maker isn't
    retried, but writes no fields and adds nothing to enriched_fields."""
    _add_maker(canonical_name=DISCOVERED_NAME, is_independent=True)
    with patch("watch_app.extraction.enrich.lookup_maker_taste", return_value={}):
        with session_scope() as session:
            written = auto_tag_taste_for_discovered(session, limit=25)
    assert written == 0
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=DISCOVERED_NAME).one()
        assert m.taste_enriched_at is not None
        assert m.maker_type is None
        assert not m.enriched_fields  # None or empty


def test_out_of_vocab_values_dropped(test_engine):
    """sanitize_taste drops anything outside the controlled vocabulary."""
    _add_maker(canonical_name=DISCOVERED_NAME, is_independent=True)
    raw = {
        "aesthetic_tags": ["modern", "steampunk"],  # steampunk is invalid
        "maker_type": "not-a-real-type",  # invalid → dropped
        "price_band": "mid",  # valid
    }
    with patch("watch_app.extraction.enrich.lookup_maker_taste", return_value=raw):
        with session_scope() as session:
            written = auto_tag_taste_for_discovered(session, limit=25)
    assert written == 2  # aesthetic_tags (filtered to ["modern"]) + price_band
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=DISCOVERED_NAME).one()
        assert m.aesthetic_tags == ["modern"]
        assert m.maker_type is None
        assert m.price_band == "mid"


def test_never_overwrites_existing_taste(test_engine):
    """A taste field that already has a value is left untouched."""
    _add_maker(
        canonical_name=DISCOVERED_NAME,
        is_independent=True,
        aesthetic_tags=["classical"],
    )
    with patch(
        "watch_app.extraction.enrich.lookup_maker_taste",
        return_value={"aesthetic_tags": ["modern"], "price_band": "high"},
    ):
        with session_scope() as session:
            written = auto_tag_taste_for_discovered(session, limit=25)
    assert written == 1  # only price_band; aesthetic_tags preserved
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=DISCOVERED_NAME).one()
        assert m.aesthetic_tags == ["classical"]
        assert m.price_band == "high"
        assert m.taste_enriched_at is not None


def test_cap_is_respected(test_engine):
    """With more taggable makers than the cap, only `limit` are processed."""
    for i in range(5):
        _add_maker(canonical_name=f"{DISCOVERED_NAME} {i}", is_independent=True)
    with patch(
        "watch_app.extraction.enrich.lookup_maker_taste", return_value={"price_band": "mid"}
    ):
        with session_scope() as session:
            auto_tag_taste_for_discovered(session, limit=2)
    with session_scope() as session:
        stamped = (
            session.query(Maker)
            .filter(Maker.taste_enriched_at.isnot(None))
            .count()
        )
    assert stamped == 2


# ---------- Dry-run propose ----------


def test_propose_makes_no_writes(test_engine):
    _add_maker(canonical_name=DISCOVERED_NAME, is_independent=True)
    with patch(
        "watch_app.extraction.enrich.lookup_maker_taste", return_value=dict(VALID_TAGS)
    ):
        with session_scope() as session:
            proposals = propose_taste_for_discovered(session)
    assert len(proposals) == 1
    assert proposals[0].tags["maker_type"] == "solo-watchmaker"
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=DISCOVERED_NAME).one()
        assert m.taste_enriched_at is None  # dry-run didn't stamp
        assert m.maker_type is None


def test_apply_after_propose_writes_and_stamps(test_engine):
    _add_maker(canonical_name=DISCOVERED_NAME, is_independent=True)
    with patch(
        "watch_app.extraction.enrich.lookup_maker_taste", return_value=dict(VALID_TAGS)
    ):
        with session_scope() as session:
            proposals = propose_taste_for_discovered(session)
            written = apply_taste_proposals(session, proposals)
    assert written == 4
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=DISCOVERED_NAME).one()
        assert m.taste_enriched_at is not None
        assert m.maker_type == "solo-watchmaker"


# ---------- 3G: context-aware prompt + retag ----------

from datetime import datetime, timezone  # noqa: E402

from watch_app.extraction.enrich import _gather_taste_context  # noqa: E402
from watch_app.storage.models import Mention, Source  # noqa: E402


def _add_maker_with_mentions(name, mention_titles, is_independent=True, **kw):
    """Insert a Maker plus a Source and one Mention per title. Returns maker id."""
    with session_scope() as session:
        src = session.query(Source).filter_by(name="TestSrc").one_or_none()
        if src is None:
            src = Source(name="TestSrc", category="media")
            session.add(src)
            session.flush()
        m = Maker(canonical_name=name, is_independent=is_independent, **kw)
        session.add(m)
        session.flush()
        for i, (title, excerpt) in enumerate(mention_titles):
            session.add(
                Mention(
                    maker_id=m.id,
                    source_id=src.id,
                    url=f"https://example.com/{name}/{i}",
                    title=title,
                    excerpt=excerpt,
                )
            )
        session.flush()
        return m.id


def test_gather_context_builds_block_from_mentions(test_engine):
    _add_maker_with_mentions(
        DISCOVERED_NAME,
        [
            ("Introducing: The Monolith Has Landed", "A next-gen tool watch."),
            ("The Monolith Takes Flight", "San Francisco-based engineer."),
        ],
    )
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=DISCOVERED_NAME).one()
        ctx = _gather_taste_context(m)
    assert ctx is not None
    assert "Monolith Has Landed" in ctx
    assert "tool watch" in ctx


def test_gather_context_none_without_mentions(test_engine):
    _add_maker(canonical_name=DISCOVERED_NAME, is_independent=True)
    with session_scope() as session:
        m = session.query(Maker).filter_by(canonical_name=DISCOVERED_NAME).one()
        assert _gather_taste_context(m) is None


def test_context_passed_into_lookup(test_engine):
    _add_maker_with_mentions(
        DISCOVERED_NAME, [("Introducing: The Monolith", "A mission-grade space watch.")]
    )
    captured = {}

    def fake_lookup(session, name, country=None, context=None):
        captured["context"] = context
        return {}

    with patch("watch_app.extraction.enrich.lookup_maker_taste", side_effect=fake_lookup):
        with session_scope() as session:
            auto_tag_taste_for_discovered(session, limit=25)
    assert captured["context"] is not None
    assert "Monolith" in captured["context"]


def test_retag_reattempts_abstained(test_engine):
    """A stamped maker with no tags is skipped by default but re-attempted
    under --retag (include_attempted=True)."""
    _add_maker(
        canonical_name=DISCOVERED_NAME,
        is_independent=True,
        taste_enriched_at=datetime.now(timezone.utc),
    )
    with session_scope() as session:
        default = [m.canonical_name for m in iter_untagged_discovered_independents(session)]
        retag = [
            m.canonical_name
            for m in iter_untagged_discovered_independents(session, include_attempted=True)
        ]
    assert DISCOVERED_NAME not in default
    assert DISCOVERED_NAME in retag


def test_retag_skips_already_tagged(test_engine):
    """--retag must not re-touch makers that already carry tags."""
    _add_maker(
        canonical_name=DISCOVERED_NAME,
        is_independent=True,
        taste_enriched_at=datetime.now(timezone.utc),
        maker_type="solo-watchmaker",
    )
    with session_scope() as session:
        retag = [
            m.canonical_name
            for m in iter_untagged_discovered_independents(session, include_attempted=True)
        ]
    assert DISCOVERED_NAME not in retag


def test_retag_end_to_end_tags_previously_abstained(test_engine):
    """Stamped + untagged maker gets tagged when re-proposed under retag."""
    mid = _add_maker(
        canonical_name=DISCOVERED_NAME,
        is_independent=True,
        taste_enriched_at=datetime.now(timezone.utc),
    )
    with patch(
        "watch_app.extraction.enrich.lookup_maker_taste",
        return_value={"maker_type": "solo-watchmaker", "price_band": "high"},
    ):
        with session_scope() as session:
            proposals = propose_taste_for_discovered(session, include_attempted=True)
            written = apply_taste_proposals(session, proposals)
    assert written == 2
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=mid).one()
        assert m.maker_type == "solo-watchmaker"
        assert m.price_band == "high"
