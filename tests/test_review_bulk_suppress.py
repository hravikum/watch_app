"""Tests for the bulk-suppress page on /review (chunk 4P).

The web audit (4O) sorts its "no" verdicts to the top of the queue; 4P lists
them on one page — the ones without a brand suggestion ticked, the ones with
one unticked — and suppresses whatever is ticked in one post. Covers:
  1. The header link and its count (queue makers judged "no" only).
  2. The checklist: who's on it, which group, what's ticked, the order.
  3. The post: suppresses the ticked, records the audit's reasoning, checks
     every id again (reviewed / suppressed / re-audited / seed / unknown are
     skipped and counted), and the notice /review shows after the redirect.
  4. Admin-only, like the rest of /review.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_app.api.routers.makers import router as makers_router
from watch_app.config import get_settings
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


SEED_NAME = "Simon Brette"  # present in the seed YAML (same anchor test_review uses)
CHECKBOX = re.compile(r'<input type="checkbox" name="maker_id" value="(\d+)"[^>]*?( checked)? />')


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


def _no(name: str, reason: str = "Not a maker.", **kwargs) -> int:
    """A queue maker the audit judged "no"."""
    return _add(name, audit_verdict="no", audit_reason=reason, audited_at=_now(), **kwargs)


def _boxes(html: str) -> dict[int, bool]:
    """maker id -> ticked, for every checkbox on the page, in page order."""
    return {int(mid): bool(checked) for mid, checked in CHECKBOX.findall(html)}


def _get(mid: int) -> Maker:
    with session_scope() as session:
        return session.get(Maker, mid)


# ---------- the link on /review ----------


def test_link_counts_queue_makers_judged_no(client, admin_on):
    _no("Zzq Junk")
    _no("Zzq Founder", audit_brand="Zzq Some Brand")
    _add("Zzq Real", audit_verdict="yes")
    _add("Zzq Maybe", audit_verdict="unsure")
    _no("Zzq Reviewed Junk", reviewed_at=_now())
    _no("Zzq Hidden Junk", is_suppressed=True)
    _no(SEED_NAME)
    r = client.get("/review")
    assert r.status_code == 200
    assert 'href="/review/bulk-suppress"' in r.text
    assert "Not a maker (2)" in r.text


def test_no_link_when_nothing_was_judged_no(client, admin_on):
    _add("Zzq Real", audit_verdict="yes")
    _add("Zzq Unaudited")
    r = client.get("/review")
    assert "/review/bulk-suppress" not in r.text


# ---------- the checklist ----------


def test_page_ticks_no_brand_and_leaves_brand_suggestions_unticked(client, admin_on):
    brand = _add("Zzq Brand Watches", audit_verdict="yes")
    plain_b = _no("zzq beta junk", reason="A retailer, not a maker.")
    plain_a = _no("Zzq Alpha Junk", reason="A strap shop.")
    founder = _no("Zzq Founder", reason="A founder.", audit_brand="Zzq Brand Watches")
    untracked = _no("Zzq Other Founder", audit_brand="Zzq Untracked Brand")
    _add("Zzq Real", audit_verdict="yes")
    _no("Zzq Reviewed Junk", reviewed_at=_now())

    r = client.get("/review/bulk-suppress")
    assert r.status_code == 200
    boxes = _boxes(r.text)
    # Alphabetical (case-insensitive) within each group; no-brand first, ticked.
    assert list(boxes) == [plain_a, plain_b, founder, untracked]
    assert boxes == {plain_a: True, plain_b: True, founder: False, untracked: False}
    assert "A retailer, not a maker." in r.text
    assert "No brand suggested" in r.text and "Brand suggested" in r.text
    assert f'href="/makers/{brand}"' in r.text  # the tracked brand is linked
    assert "not tracked yet" in r.text  # the untracked one says so
    assert "Zzq Real" not in r.text and "Zzq Reviewed Junk" not in r.text


def test_page_counts_a_brand_that_is_the_maker_itself_as_none(client, admin_on):
    """Its card shows no brand line, so it belongs with the ticked group."""
    own = _no("Zzq Self Named", audit_brand="Zzq Self Named")
    assert _boxes(client.get("/review/bulk-suppress").text) == {own: True}


def test_page_shows_sources_as_links(client, admin_on):
    _no("Zzq Junk", audit_sources=[{"url": "https://press.example/zzq", "title": "Zzq piece"}])
    r = client.get("/review/bulk-suppress")
    assert 'href="https://press.example/zzq"' in r.text
    assert ">press.example<" in r.text


def test_empty_page_when_nothing_qualifies(client, admin_on):
    _add("Zzq Real", audit_verdict="yes")
    r = client.get("/review/bulk-suppress")
    assert r.status_code == 200
    assert "cards in the queue." in r.text
    assert "<form" not in r.text


# ---------- the post ----------


def test_post_suppresses_the_ticked_with_the_audit_reason(client, admin_on):
    a = _no("Zzq Alpha Junk", reason="A strap shop.")
    b = _no("Zzq Beta Junk", reason=None)
    kept = _no("Zzq Kept")
    r = client.post(
        "/review/bulk-suppress", data={"maker_id": [a, b, a]}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/review?suppressed=2&skipped=0"
    assert _get(a).is_suppressed and _get(a).suppressed_reason == "Audit: A strap shop."
    assert _get(b).is_suppressed and _get(b).suppressed_reason == "Audit: not a maker"
    assert not _get(kept).is_suppressed
    # They leave the queue; the unticked one is still there.
    queue = client.get("/review").text
    assert "Zzq Alpha Junk" not in queue and "Zzq Kept" in queue
    assert re.search(r'id="review-to-go"[^>]*>1<', queue)


def test_post_reason_fits_the_column(client, admin_on):
    mid = _no("Zzq Wordy Junk", reason="x" * 300)
    client.post("/review/bulk-suppress", data={"maker_id": [mid]})
    reason = _get(mid).suppressed_reason
    assert reason.startswith("Audit: x") and len(reason) == 300


def test_post_skips_what_no_longer_qualifies(client, admin_on):
    """Every id is checked again: the page may be stale, and the post is
    just a list of ids."""
    ok = _no("Zzq Still Junk")
    real = _add("Zzq Re-audited Real", audit_verdict="yes")
    reviewed = _no("Zzq Reviewed Since", reviewed_at=_now())
    gone = _no("Zzq Already Gone", is_suppressed=True, suppressed_reason="Mine.")
    seed = _no(SEED_NAME)
    r = client.post(
        "/review/bulk-suppress",
        data={"maker_id": [ok, real, reviewed, gone, seed, 99999]},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/review?suppressed=1&skipped=5"
    assert _get(ok).is_suppressed
    for mid in (real, reviewed, seed):
        assert not _get(mid).is_suppressed
    assert _get(gone).suppressed_reason == "Mine."  # not overwritten


def test_ticking_the_defaults_suppresses_only_the_no_brand_group(client, admin_on):
    """The page as served: submit with the default ticks."""
    plain = _no("Zzq Junk")
    founder = _no("Zzq Founder", audit_brand="Zzq Some Brand")
    page = client.get("/review/bulk-suppress").text
    ticked = [mid for mid, on in _boxes(page).items() if on]
    client.post("/review/bulk-suppress", data={"maker_id": ticked})
    assert _get(plain).is_suppressed
    assert not _get(founder).is_suppressed


def test_notice_after_the_redirect(client, admin_on):
    _no("Zzq Junk")
    r = client.get("/review?suppressed=2&skipped=1")
    assert "Suppressed 2 makers the audit judged not to be makers." in r.text
    assert "Skipped 1 that no longer qualified" in r.text
    single = client.get("/review?suppressed=1&skipped=0").text
    assert "Suppressed 1 maker the audit" in single
    assert "Skipped" not in single
    assert "no boxes were ticked" in client.get("/review?suppressed=0&skipped=0").text
    assert 'id="bulk-suppress-result"' not in client.get("/review").text


# ---------- admin only ----------


def test_bulk_routes_404_when_admin_off(client):
    mid = _no("Zzq Junk")
    assert client.get("/review/bulk-suppress").status_code == 404
    assert client.post("/review/bulk-suppress", data={"maker_id": [mid]}).status_code == 404
    assert not _get(mid).is_suppressed
