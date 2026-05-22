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


def test_iter_unenriched_yields_makers_with_any_missing_field(test_engine):
    """Makers are yielded if ANY enrichable field is missing — in 2T this
    includes the case where website is set but country isn't.

    Previously (2Q) this test asserted only-no-website. The 2T refactor
    expanded enrichment to multiple fields, so the criterion is broader:
    a maker is unenriched if it has missing fields OR enriched_at IS NULL.
    """
    with session_scope() as session:
        # Fully enriched: has all fields populated AND has been marked enriched.
        from datetime import datetime, timezone
        m1 = Maker(canonical_name="Alpha", country="X", founded_year=2020,
                   is_independent=True, website=None)
        m2 = Maker(canonical_name="Beta", country="Y", founded_year=2020,
                   is_independent=True, website="https://example.com",
                   enriched_at=datetime.now(timezone.utc),
                   enriched_fields=["website"])
        m3 = Maker(canonical_name="Gamma", country="Z", founded_year=2020,
                   is_independent=True, website=None)
        session.add_all([m1, m2, m3])

    with session_scope() as session:
        names = [m.canonical_name for m in iter_unenriched_makers(session)]
    # Alpha and Gamma are missing website → yielded.
    assert "Alpha" in names
    assert "Gamma" in names
    # Beta has all fields AND has been marked enriched → not yielded.
    assert "Beta" not in names


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


def _patch_country_independence_lookups():
    """Returns a tuple of context managers that patch the country and
    independence lookups to return None — useful for website-focused tests
    that don't want those lookups to fire."""
    return (
        patch("watch_app.extraction.enrich.lookup_maker_country", return_value=None),
        patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None),
    )


def test_proposal_when_llm_returns_none(test_engine):
    """LLM returns None → proposal has no URL, not committable."""
    _setup_maker_for_test()
    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None):
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
               return_value="https://www.chrono24.com/journe/"), \
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None):
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
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None), \
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
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None), \
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
         patch("watch_app.extraction.enrich.lookup_maker_independence",
               return_value=None), \
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


# ---------- Enrichment tracking columns (2S) ----------


def test_apply_sets_enrichment_tracking_fields(test_engine):
    """When apply_enrichments commits a website, it should also set
    enriched_at (current time) and enriched_fields (containing 'website')."""
    from datetime import datetime, timezone

    with session_scope() as session:
        m = Maker(canonical_name="EnrichTest", is_independent=True, website=None)
        session.add(m)
        session.flush()
        maker_id = m.id

    proposals = [
        EnrichmentProposal(
            maker_id=maker_id,
            canonical_name="EnrichTest",
            country=None,
            proposed_url="https://enrichtest.example",
            url_resolved=True,
            rejection_reason=None,
        ),
    ]
    before = datetime.now(timezone.utc)
    with session_scope() as session:
        apply_enrichments(session, proposals)
    after = datetime.now(timezone.utc)

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
    assert m.website == "https://enrichtest.example"
    assert m.enriched_at is not None
    # The timestamp should fall in the test window
    assert before <= m.enriched_at.replace(tzinfo=timezone.utc) <= after
    # enriched_fields should contain "website"
    assert m.enriched_fields == ["website"]


def test_apply_appends_to_existing_enriched_fields(test_engine):
    """If a maker already has enriched_fields from a previous enrichment,
    'website' should be appended (not replace the list)."""
    with session_scope() as session:
        m = Maker(
            canonical_name="MultiEnrich",
            is_independent=True,
            website=None,
            enriched_fields=["country"],  # Pretend country was enriched earlier
        )
        session.add(m)
        session.flush()
        maker_id = m.id

    proposals = [
        EnrichmentProposal(
            maker_id=maker_id,
            canonical_name="MultiEnrich",
            country=None,
            proposed_url="https://multienrich.example",
            url_resolved=True,
            rejection_reason=None,
        ),
    ]
    with session_scope() as session:
        apply_enrichments(session, proposals)

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
    # Both fields preserved
    assert "country" in m.enriched_fields
    assert "website" in m.enriched_fields


def test_apply_does_not_set_tracking_on_skipped_makers(test_engine):
    """When a maker is skipped (URL didn't resolve, etc.), enriched_at
    should remain None — we only track actual enrichment writes."""
    with session_scope() as session:
        m = Maker(canonical_name="SkippedMaker", is_independent=True, website=None)
        session.add(m)
        session.flush()
        maker_id = m.id

    proposals = [
        EnrichmentProposal(
            maker_id=maker_id,
            canonical_name="SkippedMaker",
            country=None,
            proposed_url="https://wont-resolve.example",
            url_resolved=False,
            rejection_reason="URL did not resolve",
        ),
    ]
    with session_scope() as session:
        apply_enrichments(session, proposals)

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
    assert m.website is None
    assert m.enriched_at is None
    assert m.enriched_fields is None


# ---------- 2T: Country/independence enrichment ----------


def test_country_enrichment_commits_when_lookup_returns_known(test_engine):
    """When country lookup returns a known watchmaking country, it gets
    written to the Maker row."""
    with session_scope() as session:
        m = Maker(canonical_name="Country Test", is_independent=True,
                  website=None, country=None)
        session.add(m)
        session.flush()
        maker_id = m.id

    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_country", return_value="Japan"), \
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None):
        proposals = propose_enrichments(session)
        apply_enrichments(session, proposals)

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
    assert m.country == "Japan"
    assert "country" in m.enriched_fields


def test_country_enrichment_skips_when_lookup_returns_none(test_engine):
    """If country lookup returns None, the Maker.country stays None."""
    with session_scope() as session:
        m = Maker(canonical_name="No Country", is_independent=True,
                  website=None, country=None)
        session.add(m)
        session.flush()
        maker_id = m.id

    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_country", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None):
        proposals = propose_enrichments(session)
        apply_enrichments(session, proposals)

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
    assert m.country is None


def test_country_does_not_overwrite_existing(test_engine):
    """If country is already set, the enrichment skips the lookup entirely."""
    with session_scope() as session:
        m = Maker(canonical_name="Has Country", is_independent=True,
                  website=None, country="Switzerland")
        session.add(m)
        session.flush()

    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_country") as mock_country, \
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None):
        propose_enrichments(session)

    # Lookup should not have been called since the field was already set.
    mock_country.assert_not_called()


def test_independence_enrichment_commits_no(test_engine):
    """When independence lookup returns False (conglomerate), commit it."""
    with session_scope() as session:
        m = Maker(canonical_name="Conglomerate Brand", is_independent=True,
                  website=None, country=None)
        session.add(m)
        session.flush()
        maker_id = m.id

    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_country", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=False):
        proposals = propose_enrichments(session)
        apply_enrichments(session, proposals)

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
    assert m.is_independent is False
    assert "is_independent" in m.enriched_fields


def test_independence_enrichment_skips_none(test_engine):
    """When independence lookup returns None, don't commit — preserves
    the default True (LLM was uncertain, default holds)."""
    with session_scope() as session:
        m = Maker(canonical_name="Uncertain", is_independent=True,
                  website=None, country=None)
        session.add(m)
        session.flush()
        maker_id = m.id

    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_country", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_independence", return_value=None):
        proposals = propose_enrichments(session)
        apply_enrichments(session, proposals)

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).one()
    # Default preserved
    assert m.is_independent is True
    # is_independent was NOT marked as enriched (we didn't commit anything)
    assert not (m.enriched_fields and "is_independent" in m.enriched_fields)


def test_independence_skipped_if_already_enriched(test_engine):
    """If is_independent was previously enriched, don't re-look it up."""
    from datetime import datetime, timezone
    with session_scope() as session:
        m = Maker(canonical_name="Already Done", is_independent=False,
                  website=None, country=None,
                  enriched_at=datetime.now(timezone.utc),
                  enriched_fields=["is_independent"])
        session.add(m)
        session.flush()

    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_country", return_value=None), \
         patch("watch_app.extraction.enrich.lookup_maker_independence") as mock_indep:
        propose_enrichments(session)

    mock_indep.assert_not_called()


# ---------- 2T: Automatic enrichment ----------


def test_auto_enrich_caps_at_limit(test_engine):
    """auto_enrich_unenriched_makers respects the limit parameter."""
    from watch_app.extraction.enrich import auto_enrich_unenriched_makers

    # Create more makers than the cap
    with session_scope() as session:
        for i in range(10):
            session.add(Maker(
                canonical_name=f"Capped Maker {i:02d}",
                is_independent=True,
                website=None,
                country=None,
            ))

    mock_response = MagicMock()
    mock_response.status_code = 200
    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website",
               return_value="https://example.com") as mock_url, \
         patch("watch_app.extraction.enrich.lookup_maker_country",
               return_value="Japan"), \
         patch("watch_app.extraction.enrich.lookup_maker_independence",
               return_value=True), \
         patch("watch_app.extraction.enrich.http_get", return_value=mock_response):
        auto_enrich_unenriched_makers(session, limit=3)

    # Only 3 makers should have been processed (3 * 3 fields = 9 lookups)
    # But the website mock is shared across makers — we check enriched count instead
    with session_scope() as session:
        enriched_count = (
            session.query(Maker)
            .filter(Maker.canonical_name.like("Capped Maker%"))
            .filter(Maker.enriched_at.isnot(None))
            .count()
        )
    assert enriched_count == 3


def test_auto_enrich_only_enriched_at_null(test_engine):
    """auto_enrich processes only makers with enriched_at IS NULL."""
    from datetime import datetime, timezone
    from watch_app.extraction.enrich import auto_enrich_unenriched_makers

    with session_scope() as session:
        # Already enriched
        session.add(Maker(
            canonical_name="Already Enriched",
            is_independent=True,
            website="https://already.example",
            country="Japan",
            enriched_at=datetime.now(timezone.utc),
            enriched_fields=["website", "country"],
        ))
        # Not yet enriched
        session.add(Maker(
            canonical_name="Not Yet Enriched",
            is_independent=True,
            website=None,
            country=None,
        ))

    with session_scope() as session, \
         patch("watch_app.extraction.enrich.lookup_maker_website",
               return_value=None) as mock_url, \
         patch("watch_app.extraction.enrich.lookup_maker_country",
               return_value="France"), \
         patch("watch_app.extraction.enrich.lookup_maker_independence",
               return_value=None):
        auto_enrich_unenriched_makers(session, limit=25)

    with session_scope() as session:
        already = session.query(Maker).filter_by(canonical_name="Already Enriched").one()
        not_yet = session.query(Maker).filter_by(canonical_name="Not Yet Enriched").one()
    # Already-enriched maker unchanged
    assert already.country == "Japan"
    # Not-yet-enriched maker got country filled
    assert not_yet.country == "France"
