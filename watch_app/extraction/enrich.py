"""Post-discovery enrichment of Maker rows.

When the resolver auto-discovers a maker from a mention, the resulting
Maker row has no website, country, or other metadata. This module fills
the gap for the `website` field specifically, via LLM lookup + HTTP
verification.

Workflow:
  1. Find Makers where website IS NULL
  2. For each, ask Claude for the official URL (cached, so re-running is free)
  3. Verify the URL actually resolves (HTTP 2xx or 3xx) before committing
  4. Reject results from known aggregator/dealer domains
  5. In dry-run mode, print proposals without writing
  6. In commit mode, write the validated URLs to the DB

Design notes:
- Default is dry-run. Users must pass --commit to actually write.
- Never overwrites existing data — only fills None.
- LLM results are cached, so dry-run + commit doesn't double-bill.
- One source of truth for "is this URL acceptable": the
  _is_acceptable_url function. Failures are logged and skipped, not
  raised — one bad lookup shouldn't abort enrichment of 28 others.

The cost ceiling: ~$0.001 per maker enriched (one Haiku call, ~600
tokens total). Backfilling 28 makers costs ~$0.02 once.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from watch_app.http import get as http_get
from watch_app.llm.client import lookup_maker_website
from watch_app.storage.models import Maker


logger = logging.getLogger(__name__)


#: Domains where a URL is almost certainly NOT a maker's official website.
#: We reject these even if the LLM proposes them.
BLOCKED_DOMAINS = frozenset(
    [
        # Aggregators / marketplaces
        "chrono24.com",
        "watchcharts.com",
        "watchcollecting.com",
        # Forums
        "watchuseek.com",
        "reddit.com",
        "instagram.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "tiktok.com",
        # Wiki / general reference
        "wikipedia.org",
        "wikidata.org",
        # Major dealers (not exhaustive; just the most common)
        "hodinkee.com",
        "ablogtowatch.com",
        "watchesbysjx.com",
        "monochrome-watches.com",
        "fratellowatches.com",
        "quillandpad.com",
        "worldtempus.com",
    ]
)


@dataclass
class EnrichmentProposal:
    """One proposed change to a Maker row, with verification result."""

    maker_id: int
    canonical_name: str
    country: str | None
    proposed_url: str | None
    url_resolved: bool  # Did the URL actually return 2xx/3xx?
    rejection_reason: str | None  # Why we'd skip this (if any)

    @property
    def is_committable(self) -> bool:
        return (
            self.proposed_url is not None
            and self.url_resolved
            and self.rejection_reason is None
        )


def _is_acceptable_url(url: str) -> str | None:
    """Return rejection reason or None if URL is acceptable."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unexpected scheme {parsed.scheme!r}"
    host = (parsed.hostname or "").lower()
    if not host:
        return "missing hostname"
    # Strip "www." for blocklist comparison
    host_no_www = host[4:] if host.startswith("www.") else host
    if host_no_www in BLOCKED_DOMAINS:
        return f"blocked domain {host_no_www}"
    return None


def _verify_url_resolves(url: str) -> bool:
    """Return True if the URL returns a non-error HTTP status.

    Many maker sites are slow or have aggressive bot-blocking, so we
    use a generous timeout via http_get's existing per-phase timeouts.
    Anything 2xx/3xx counts as "alive enough." We don't follow links
    or check page content.
    """
    try:
        response = http_get(url)
        return response.status_code < 400
    except Exception as exc:
        logger.info("URL %s did not resolve: %s", url, exc)
        return False


def iter_unenriched_makers(session: Session) -> Iterator[Maker]:
    """Yield Maker rows where website is None.

    Ordered by canonical_name for stable dry-run output.
    """
    yield from (
        session.query(Maker)
        .filter(Maker.website.is_(None))
        .order_by(Maker.canonical_name)
        .all()
    )


def propose_enrichments(session: Session) -> list[EnrichmentProposal]:
    """Compute proposed enrichments for every unenriched Maker.

    Calls the LLM (cached) and verifies each proposed URL via HTTP.
    Does NOT write to the database. Caller decides whether to commit.
    """
    proposals: list[EnrichmentProposal] = []
    for maker in iter_unenriched_makers(session):
        url = lookup_maker_website(
            session, maker.canonical_name, country=maker.country
        )
        if url is None:
            proposals.append(
                EnrichmentProposal(
                    maker_id=maker.id,
                    canonical_name=maker.canonical_name,
                    country=maker.country,
                    proposed_url=None,
                    url_resolved=False,
                    rejection_reason="LLM returned NONE",
                )
            )
            continue

        rejection = _is_acceptable_url(url)
        if rejection:
            proposals.append(
                EnrichmentProposal(
                    maker_id=maker.id,
                    canonical_name=maker.canonical_name,
                    country=maker.country,
                    proposed_url=url,
                    url_resolved=False,
                    rejection_reason=rejection,
                )
            )
            continue

        resolved = _verify_url_resolves(url)
        proposals.append(
            EnrichmentProposal(
                maker_id=maker.id,
                canonical_name=maker.canonical_name,
                country=maker.country,
                proposed_url=url,
                url_resolved=resolved,
                rejection_reason=None if resolved else "URL did not resolve",
            )
        )
    return proposals


def apply_enrichments(
    session: Session, proposals: list[EnrichmentProposal]
) -> int:
    """Write committable proposals to the database.

    Skips proposals that have a rejection_reason or whose URLs didn't
    resolve. Returns the count written.

    Defensive: re-checks `Maker.website is None` before writing in case
    the row was modified between propose and apply (rare, but possible
    in long-running enrichment runs).
    """
    count = 0
    for p in proposals:
        if not p.is_committable:
            continue
        maker = session.query(Maker).filter_by(id=p.maker_id).first()
        if maker is None:
            logger.warning("Maker id=%d disappeared between propose and apply", p.maker_id)
            continue
        if maker.website is not None:
            logger.info(
                "Skipping %s: website was set between propose and apply",
                maker.canonical_name,
            )
            continue
        maker.website = p.proposed_url
        count += 1
    session.commit()
    return count
