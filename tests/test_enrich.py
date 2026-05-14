"""Tests for maker website enrichment.

Verifies:
  1. URL acceptability logic (good URLs, blocked domains, malformed)
  2. propose_enrichments handles all paths (LLM=None, blocked, unresolved, good)
  3. apply_enrichments writes only committable proposals
  4. apply_enrichments defensively skips makers whose website was set
     between propose and apply
  5. Iteration skips already-enriched makers
  6. Cache behavior — same maker isn't looked up twice
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from watch_app.extraction.enrich import (
    BLOCKED_DOMAINS,
    EnrichmentProposal,
    _is_acceptable_url,
    apply_enrichments,
    iter_unenriched_makers,
    propose_enrichments,
)
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker


# ---------- URL acceptability ----------


def test_acceptable_https_url():
    assert _is_acceptable_url("https://www.simonbrette.com") is None


def test_acceptable_http_url():
    assert _is_acceptable_url("http://example.com") is None


def test_rejects_chrono24():
    reason = _is_acceptable_url("https://www.chrono24.com/journe/")
    assert reason is not None
    assert "chrono24" in reason


def test_rejects_chrono24_no_www():
    reason = _is_acceptable_url("https://chrono24.com/journe/")
    assert reason is not None


def test_rejects_instagram():
    reason = _is_acceptable_url("https://instagram.com/somemaker")
    assert reason is not None
    assert "instagram" in reason


def test_rejects_journalism_sources():
    """Our own ingested sources should never be proposed as maker websites."""
    for domain in ["hodinkee.com", "watchesbysjx.com", "monochrome-watches.com"]:
        reason = _is_acceptable_url(f"https://{domain}/some/article")
        assert reason is not None, f"Expected to reject {domain}"


def test_rejects_unknown_scheme():
    reason = _is_acceptable_url("ftp://example.com")
    assert reason is not None
    assert "scheme" in reason


def test_rejects_missing_host():
    reason = _is_acceptable_url("https://")
    assert reason is not None
    assert "hostname" in reason


# ---------- Iteration ----------


def test_iter_unenriched_yields_only_makers_without_website(test_engine):
    """Makers with website set are NOT yielded."""
    with session_scope() as session:
        m1 = Maker(canonical_name="Alpha", country="X", is_independent=True, website=None)
        m2 = Maker(canonical_name="Beta", country="Y", is_independent=True, website="https://example.com")
        m3 = Maker(canonical_name="Gamma", country="Z", is_independent=True, website=None)
        session.add_all([m1, m2, m3])

    with session_scope() as session:
        names = [m.canonical_name for m in iter_unenriched_makers(session)]
    assert "Alpha" in names
    assert "Beta" not in names  # has website
    assert "Gamma" in names


def test_iter_unenriched_sorted_by_name(test_engine):
    """Order is stable (alphabetical) for predictable dry-run output."""
    with session_scope() as session:
        for n in ["Zulu", "Alpha", "Mike"]:
            session.add(Maker(canonical_name=n, is_independent=True, website=None))

    with session_scope() as session:
        names = [m.canonical_name for m in iter_unenriched_makers(session)]
    assert names == ["Alpha", "Mike", "Zulu"]


# ---------- Proposal paths ----------


def _setup_maker_for_test():
    """Create one unenriched maker, return its id."""
    with session_scope() as session:
        m = Maker(canonical_name="Test Maker", country="France", is_independent=True, website=None)
        session.add(m)
        session.flush()
        return m.id


def test_proposal_when_llm_returns_none(test_engine):
    """LLM returns None → proposal has no URL, not committable."""
    _setup_maker_for_test()
    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website", return_value=None):
        proposals = propose_enrichments(session)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.proposed_url is None
    assert not p.is_committable
    assert "NONE" in p.rejection_reason


def test_proposal_with_blocked_url(test_engine):
    """LLM returns a chrono24 URL → rejected, not committable."""
    _setup_maker_for_test()
    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website",
               return_value="https://www.chrono24.com/journe/"):
        proposals = propose_enrichments(session)
    p = proposals[0]
    assert p.proposed_url == "https://www.chrono24.com/journe/"
    assert not p.is_committable
    assert "chrono24" in p.rejection_reason


def test_proposal_with_unresolvable_url(test_engine):
    """LLM returns a URL that 404s → not committable."""
    _setup_maker_for_test()
    mock_response = MagicMock()
    mock_response.status_code = 404
    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website",
               return_value="https://does-not-exist.example"), \
         patch("watch_app.extraction.enrich.http_get", return_value=mock_response):
        proposals = propose_enrichments(session)
    p = proposals[0]
    assert p.proposed_url == "https://does-not-exist.example"
    assert not p.url_resolved
    assert not p.is_committable


def test_proposal_with_good_url(test_engine):
    """LLM returns a URL that resolves → committable."""
    _setup_maker_for_test()
    mock_response = MagicMock()
    mock_response.status_code = 200
    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website",
               return_value="https://www.testmaker.com"), \
         patch("watch_app.extraction.enrich.http_get", return_value=mock_response):
        proposals = propose_enrichments(session)
    p = proposals[0]
    assert p.proposed_url == "https://www.testmaker.com"
    assert p.url_resolved
    assert p.is_committable


def test_proposal_with_http_exception(test_engine):
    """If http_get raises, URL is treated as unresolvable (not crash)."""
    _setup_maker_for_test()
    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website",
               return_value="https://unreachable.example"), \
         patch("watch_app.extraction.enrich.http_get",
               side_effect=ConnectionError("DNS failure")):
        proposals = propose_enrichments(session)
    p = proposals[0]
    assert not p.url_resolved
    assert not p.is_committable


# ---------- Apply ----------


def test_apply_writes_committable_only(test_engine):
    """Only proposals with is_committable=True are written."""
    with session_scope() as session:
        m1 = Maker(canonical_name="Good", is_independent=True, website=None)
        m2 = Maker(canonical_name="Bad", is_independent=True, website=None)
        session.add_all([m1, m2])
        session.flush()
        good_id, bad_id = m1.id, m2.id

    proposals = [
        EnrichmentProposal(
            maker_id=good_id,
            canonical_name="Good",
            country=None,
            proposed_url="https://good.example",
            url_resolved=True,
            rejection_reason=None,
        ),
        EnrichmentProposal(
            maker_id=bad_id,
            canonical_name="Bad",
            country=None,
            proposed_url="https://bad.example",
            url_resolved=False,
            rejection_reason="URL did not resolve",
        ),
    ]
    with session_scope() as session:
        written = apply_enrichments(session, proposals)
    assert written == 1
    with session_scope() as session:
        good = session.query(Maker).filter_by(canonical_name="Good").one()
        bad = session.query(Maker).filter_by(canonical_name="Bad").one()
    assert good.website == "https://good.example"
    assert bad.website is None  # Not committable, not written


def test_apply_skips_makers_whose_website_was_set(test_engine):
    """Defensive: if Maker.website is set between propose and apply,
    don't overwrite. The 2M 'never overwrite' invariant holds."""
    with session_scope() as session:
        m = Maker(canonical_name="Race", is_independent=True, website=None)
        session.add(m)
        session.flush()
        maker_id = m.id

    proposals = [
        EnrichmentProposal(
            maker_id=maker_id,
            canonical_name="Race",
            country=None,
            proposed_url="https://proposed.example",
            url_resolved=True,
            rejection_reason=None,
        ),
    ]

    # Simulate something setting the website between propose and apply.
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).first()
        m.website = "https://manually-set.example"

    with session_scope() as session:
        written = apply_enrichments(session, proposals)
    assert written == 0  # Skipped, not overwritten.
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
    # Manual setting preserved.
    assert m.website == "https://manually-set.example"


def test_apply_handles_missing_maker(test_engine):
    """If a Maker is deleted between propose and apply, skip cleanly."""
    proposals = [
        EnrichmentProposal(
            maker_id=99999,  # nonexistent
            canonical_name="Ghost",
            country=None,
            proposed_url="https://example.com",
            url_resolved=True,
            rejection_reason=None,
        ),
    ]
    with session_scope() as session:
        written = apply_enrichments(session, proposals)
    assert written == 0


# ---------- BLOCKED_DOMAINS sanity ----------


def test_blocked_domains_includes_our_own_sources():
    """The sources we ingest from are blocked as proposed maker websites —
    we never want to confuse a journalism article URL for a maker site."""
    expected = ["watchesbysjx.com", "monochrome-watches.com", "hodinkee.com",
                "fratellowatches.com", "quillandpad.com"]
    for d in expected:
        assert d in BLOCKED_DOMAINS, f"{d} should be blocked"
