"""Tests for the /review queue (chunk 4N).

4N turned /review from an editor into a queue: Save stamps reviewed_at and the
maker leaves the queue; each card leads with its evidence and a summary of its
current values; Save / Suppress / Delete / Promote answer HTMX requests with a
collapsed row (or an inline blocker) instead of reloading the grid; and
enrichment never touches a reviewed maker. Covers:
  1. Queue membership, the reviewed toggle, and the "to go" / reviewed counts.
  2. Ordering — ranked first by rank, then unranked newest-first; the
     reviewed view most-recently-reviewed first.
  3. Evidence (mention count, sources, latest distinct titles) and the value
     summary (auto flags, blanks, default independence).
  4. Save: a plain post stamps reviewed_at; the HTMX success and blocker paths.
  5. HTMX suppress / delete / promote, and the confirm-delete fragment.
  6. Enrichment skips reviewed makers — including the regression where
     `cli enrich --commit` undid a reviewed independence call and refilled a
     cleared website from the LLM cache.
The plain-form (no-JS) paths stay covered by test_review.py,
test_review_maintenance.py, test_rename.py and test_promote.py.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import (
    _fetch_discovered_for_review,
    _review_counts,
    _review_evidence,
    router as makers_router,
)
from watch_app.config import get_settings
from watch_app.extraction import enrich, promote
from watch_app.extraction.enrich import (
    apply_enrichments,
    iter_unenriched_makers,
    iter_untagged_discovered_independents,
    propose_enrichments,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score, Source


DISC = "Zzq Queue Maker"
SEED_NAME = "Simon Brette"  # present in the seed YAML (same anchor test_review uses)
HX = {"HX-Request": "true"}


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


@pytest.fixture
def admin_on(monkeypatch):
    monkeypatch.setenv("WATCH_APP_ADMIN", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tmp_seed(tmp_path, monkeypatch):
    """Point promote at a temp copy of the seed so the real YAML is never
    written (same approach as test_promote.py)."""
    dest = tmp_path / "known_makers.yaml"
    shutil.copy(promote.SEED_PATH, dest)
    monkeypatch.setattr("watch_app.extraction.seed.SEED_PATH", dest)
    monkeypatch.setattr("watch_app.extraction.promote.SEED_PATH", dest)
    return dest


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add(name: str, **kwargs) -> int:
    defaults = {"is_independent": True}
    defaults.update(kwargs)
    with session_scope() as session:
        m = Maker(canonical_name=name, **defaults)
        session.add(m)
        session.flush()
        return m.id


def _with_mentions() -> int:
    """DISC with five mentions across two sources, one headline repeated."""
    with session_scope() as session:
        a = Source(name="Zzq Src A", category="media", prestige_weight=0.5)
        b = Source(name="Zzq Src B", category="media", prestige_weight=0.5)
        m = Maker(canonical_name=DISC, is_independent=True)
        session.add_all([a, b, m])
        session.flush()
        for src, title, days, url in [
            (a, "Oldest piece", 40, "http://x/1"),
            (b, "Same headline", 10, "http://x/2"),
            (a, "Same headline", 9, "http://x/3"),
            (b, "Newest piece", 1, "http://x/4"),
            (a, "Middle piece", 20, "http://x/5"),
        ]:
            session.add(
                Mention(
                    maker_id=m.id,
                    source_id=src.id,
                    url=url,
                    title=title,
                    published_at=_now() - timedelta(days=days),
                )
            )
        return m.id


# ---------- queue membership + counts ----------


def test_queue_lists_unreviewed_only(client, admin_on):
    _add("Zzq Waiting")
    _add("Zzq Done", reviewed_at=_now())
    r = client.get("/review")
    assert r.status_code == 200
    assert "Zzq Waiting" in r.text
    assert "Zzq Done" not in r.text


def test_reviewed_toggle_lists_reviewed_only(client, admin_on):
    _add("Zzq Waiting")
    _add("Zzq Done", reviewed_at=_now())
    r = client.get("/review?show=reviewed")
    assert r.status_code == 200
    assert "Zzq Done" in r.text
    assert "Zzq Waiting" not in r.text


def test_counts_cover_discovered_unsuppressed_only(test_engine):
    _add("Zzq A")
    _add("Zzq B")
    _add("Zzq C", reviewed_at=_now())
    _add("Zzq Hidden", is_suppressed=True)
    _add(SEED_NAME)
    with session_scope() as session:
        assert _review_counts(session) == (2, 1)


def test_header_shows_counts(client, admin_on):
    _add("Zzq A")
    _add("Zzq B")
    _add("Zzq C", reviewed_at=_now())
    r = client.get("/review")
    assert re.search(r'id="review-to-go"[^>]*>2<', r.text)
    assert "Reviewed (1)" in r.text


def test_empty_queue_says_clear_when_all_reviewed(client, admin_on):
    _add("Zzq Done", reviewed_at=_now())
    r = client.get("/review")
    assert r.status_code == 200
    assert "Queue's clear" in r.text


# ---------- ordering ----------


def test_queue_orders_ranked_then_unranked_newest_first(test_engine):
    _add("Zzq Old Unranked", first_seen_at=_now() - timedelta(days=30))
    _add("Zzq New Unranked", first_seen_at=_now() - timedelta(days=1))
    two = _add("Zzq Ranked Two", first_seen_at=_now() - timedelta(days=60))
    one = _add("Zzq Ranked One", first_seen_at=_now() - timedelta(days=90))
    preset = get_settings().default_preset
    with session_scope() as session:
        for mid, rank in [(one, 1), (two, 2)]:
            session.add(
                Score(maker_id=mid, preset=preset, value=1.0, rank=rank, computed_at=_now())
            )
    names = [d["canonical_name"] for d in _fetch_discovered_for_review()]
    assert names == [
        "Zzq Ranked One",
        "Zzq Ranked Two",
        "Zzq New Unranked",
        "Zzq Old Unranked",
    ]


def test_reviewed_view_orders_most_recent_first(test_engine):
    _add("Zzq Reviewed Earlier", reviewed_at=_now() - timedelta(days=5))
    _add("Zzq Reviewed Later", reviewed_at=_now() - timedelta(hours=1))
    names = [d["canonical_name"] for d in _fetch_discovered_for_review(reviewed=True)]
    assert names == ["Zzq Reviewed Later", "Zzq Reviewed Earlier"]


# ---------- evidence + summary ----------


def test_evidence_counts_sources_and_latest_distinct_titles(test_engine):
    mid = _with_mentions()
    with session_scope() as session:
        ev = _review_evidence(session, [mid])[mid]
    assert ev["mention_count"] == 5
    assert ev["sources"] == ["Zzq Src A", "Zzq Src B"]
    # Newest first, the repeated headline once, capped at three.
    assert [r["title"] for r in ev["recent"]] == [
        "Newest piece",
        "Same headline",
        "Middle piece",
    ]


def test_card_renders_evidence_and_website_link(client, admin_on):
    mid = _with_mentions()
    with session_scope() as session:
        session.get(Maker, mid).website = "https://zzq-maker.example"
    r = client.get("/review")
    assert "5 mentions" in r.text
    assert 'href="http://x/4"' in r.text and "Newest piece" in r.text
    assert 'href="https://zzq-maker.example"' in r.text
    assert "zzq-maker.example ↗" in r.text


def test_summary_flags_auto_values_and_lists_blanks(test_engine):
    _add(DISC, country="Japan", maker_type="microbrand", enriched_fields=["maker_type"])
    [row] = _fetch_discovered_for_review()
    by_label = {s["label"]: s for s in row["summary"]}
    assert by_label["Type"]["text"] == "Microbrand"
    assert by_label["Type"]["auto"] is True
    assert by_label["Country"]["text"] == "Japan"
    assert by_label["Country"]["auto"] is False
    assert {"Founded", "Website", "Price band"} <= set(row["blank"])
    assert "Type" not in row["blank"]


def test_summary_marks_default_independence(test_engine):
    """is_independent defaults to True, so an unreviewed maker whose independence
    nothing determined must not read as a checked "Yes"."""
    _add("Zzq Default Indep")
    _add("Zzq Enriched Indep", enriched_fields=["is_independent"])
    _add("Zzq Not Indep", is_independent=False)
    _add("Zzq Reviewed Indep", reviewed_at=_now())

    def indep(rows, name):
        row = next(d for d in rows if d["canonical_name"] == name)
        return next(s["text"] for s in row["summary"] if s["label"] == "Independent")

    queue = _fetch_discovered_for_review()
    assert indep(queue, "Zzq Default Indep") == "Yes (default)"
    assert indep(queue, "Zzq Enriched Indep") == "Yes"
    assert indep(queue, "Zzq Not Indep") == "No"
    assert indep(_fetch_discovered_for_review(reviewed=True), "Zzq Reviewed Indep") == "Yes"


def test_card_wires_htmx_actions(client, admin_on):
    mid = _add(DISC)
    r = client.get("/review")
    for attr in (
        f'id="review-card-{mid}"',
        f'id="review-msg-{mid}"',
        f'hx-post="/review/maker/{mid}"',
        f'hx-post="/review/suppress/{mid}"',
        f'hx-post="/review/promote/{mid}"',
        f'hx-get="/review/confirm-delete/{mid}"',
    ):
        assert attr in r.text


# ---------- save ----------


def test_plain_save_stamps_reviewed_and_leaves_queue(client, admin_on):
    mid = _add(DISC)
    r = client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": DISC, "is_independent": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with session_scope() as session:
        assert session.get(Maker, mid).reviewed_at is not None
    assert DISC not in client.get("/review").text
    assert DISC in client.get("/review?show=reviewed").text


def test_htmx_save_returns_done_row_and_count(client, admin_on):
    mid = _add(DISC, maker_type="microbrand", enriched_fields=["maker_type"])
    _add("Zzq Still Waiting")
    r = client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": DISC, "maker_type": "microbrand", "is_independent": "on"},
        headers=HX,
    )
    assert r.status_code == 200  # a fragment, not the 303
    assert "Saved" in r.text and DISC in r.text
    assert re.search(r'id="review-to-go"[^>]*hx-swap-oob="true"[^>]*>1<', r.text)
    with session_scope() as session:
        m = session.get(Maker, mid)
        assert m.reviewed_at is not None
        assert m.maker_type == "microbrand"
        assert not m.enriched_fields  # the accepted draft is no longer "auto"


def test_htmx_save_rename_collision_is_inline_and_saves_nothing(client, admin_on):
    _add("Zzq Taken Name")
    mid = _add(DISC)
    r = client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": "Zzq Taken Name", "keep_old_name": "on", "country": "Japan"},
        headers=HX,
    )
    assert r.status_code == 200
    assert r.headers["HX-Retarget"] == f"#review-msg-{mid}"
    assert r.headers["HX-Reswap"] == "innerHTML"
    assert "Merge" in r.text
    with session_scope() as session:
        m = session.get(Maker, mid)
        assert m.canonical_name == DISC
        assert m.country is None
        assert m.reviewed_at is None


def test_htmx_save_out_of_vocab_is_inline(client, admin_on):
    mid = _add(DISC)
    r = client.post(
        f"/review/maker/{mid}", data={"maker_type": "not-a-real-type"}, headers=HX
    )
    assert r.status_code == 200
    assert r.headers["HX-Retarget"] == f"#review-msg-{mid}"
    assert "not-a-real-type" in r.text
    with session_scope() as session:
        assert session.get(Maker, mid).reviewed_at is None


def test_htmx_save_missing_maker_is_inline(client, admin_on):
    r = client.post(
        "/review/maker/99999", data={"maker_type": "solo-watchmaker"}, headers=HX
    )
    assert r.status_code == 200
    assert r.headers["HX-Retarget"] == "#review-msg-99999"


# ---------- suppress / delete / promote over HTMX ----------


def test_htmx_suppress_collapses_card(client, admin_on):
    mid = _add(DISC)
    r = client.post(f"/review/suppress/{mid}", headers=HX)
    assert r.status_code == 200
    assert "Suppressed" in r.text
    assert re.search(r'id="review-to-go"[^>]*>0<', r.text)
    with session_scope() as session:
        assert session.get(Maker, mid).is_suppressed is True


def test_confirm_delete_fragment_shows_dry_run(client, admin_on):
    mid = _with_mentions()
    r = client.get(f"/review/confirm-delete/{mid}")
    assert r.status_code == 200
    assert "Permanently delete" in r.text
    assert "5 mentions" in r.text
    assert f'hx-post="/review/delete/{mid}"' in r.text
    # Inline Cancel clears the panel instead of reloading the page.
    assert f"getElementById('review-panel-{mid}')" in r.text
    with session_scope() as session:
        assert session.get(Maker, mid) is not None  # dry-run only


def test_confirm_delete_fragment_404_when_admin_off(client):
    mid = _add(DISC)
    assert client.get(f"/review/confirm-delete/{mid}").status_code == 404


def test_htmx_delete_collapses_card_without_link(client, admin_on):
    mid = _with_mentions()
    r = client.post(f"/review/delete/{mid}", headers=HX)
    assert r.status_code == 200
    assert "Deleted" in r.text
    assert f'href="/makers/{mid}"' not in r.text  # its page is gone
    with session_scope() as session:
        assert session.get(Maker, mid) is None


def test_htmx_delete_seed_maker_blocked_inline(client, admin_on):
    mid = _add(SEED_NAME)
    r = client.post(f"/review/delete/{mid}", headers=HX)
    assert r.status_code == 200
    assert r.headers["HX-Retarget"] == f"#review-msg-{mid}"
    assert "seed maker" in r.text
    with session_scope() as session:
        assert session.get(Maker, mid) is not None


def test_htmx_promote_collapses_card(client, admin_on, tmp_seed):
    mid = _add("Zzz Queue Route Maker", maker_type="solo-watchmaker")
    r = client.post(f"/review/promote/{mid}", headers=HX)
    assert r.status_code == 200
    assert "Promoted to seed" in r.text
    assert "Zzz Queue Route Maker" in tmp_seed.read_text()


def test_htmx_promote_collision_is_inline(client, admin_on, tmp_seed):
    mid = _add(SEED_NAME, maker_type="solo-watchmaker")
    before = tmp_seed.read_text()
    r = client.post(f"/review/promote/{mid}", headers=HX)
    assert r.status_code == 200
    assert r.headers["HX-Retarget"] == f"#review-msg-{mid}"
    assert "already matches seed entry" in r.text
    assert tmp_seed.read_text() == before


# ---------- enrichment leaves reviewed makers alone ----------


def test_factual_enrichment_skips_reviewed_makers(test_engine):
    _add("Zzq Unreviewed")
    _add("Zzq Reviewed", reviewed_at=_now())
    with session_scope() as session:
        manual = [m.canonical_name for m in iter_unenriched_makers(session)]
        on_scan = [
            m.canonical_name
            for m in iter_unenriched_makers(session, only_auto_enrichable=True)
        ]
    assert "Zzq Unreviewed" in manual and "Zzq Unreviewed" in on_scan
    assert "Zzq Reviewed" not in manual and "Zzq Reviewed" not in on_scan


def test_taste_tagging_skips_reviewed_makers(test_engine):
    """Covers both gates — including --retag, which would otherwise redraft
    taste a reviewer deliberately left blank."""
    _add("Zzq Unreviewed")
    _add("Zzq Reviewed", reviewed_at=_now(), taste_enriched_at=_now())
    with session_scope() as session:
        default = [m.canonical_name for m in iter_untagged_discovered_independents(session)]
        retag = [
            m.canonical_name
            for m in iter_untagged_discovered_independents(session, include_attempted=True)
        ]
    assert "Zzq Unreviewed" in default and "Zzq Unreviewed" in retag
    assert "Zzq Reviewed" not in default and "Zzq Reviewed" not in retag


class _Resolved:
    status_code = 200


def test_cli_enrich_commit_keeps_reviewed_values(client, admin_on, monkeypatch):
    """Regression found while scoping 4N. Before reviewed_at, `cli enrich
    --commit` swept a reviewed maker back in (its founded year was blank),
    re-proposed is_independent — saving drops the auto marker that was its only
    guard — and refilled a website the reviewer had cleared with the same
    cached LLM answer, marking both auto again."""
    mid = _add(
        DISC,
        website="https://wrong.example",
        enriched_at=_now(),
        enriched_fields=["is_independent", "website"],
    )
    # Review: it's group-owned, and the website is wrong.
    r = client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": DISC, "website": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Later, `cli enrich --commit`, with the LLM cache returning its first answers.
    monkeypatch.setattr(enrich, "lookup_maker_independence", lambda *a, **k: True)
    monkeypatch.setattr(enrich, "lookup_maker_website", lambda *a, **k: "https://wrong.example")
    monkeypatch.setattr(enrich, "http_get", lambda url: _Resolved())
    with session_scope() as session:
        written = apply_enrichments(session, propose_enrichments(session))

    assert written == 0
    with session_scope() as session:
        m = session.get(Maker, mid)
        assert m.is_independent is False
        assert m.website is None
        assert not m.enriched_fields
