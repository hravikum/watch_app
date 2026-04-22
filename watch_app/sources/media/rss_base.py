"""Generic RSS/Atom source base class.

Handles everything that's the same across every feed:
- Fetch the feed URL via our cached, rate-limited HTTP client
- Parse with feedparser (handles RSS 2.0, Atom, and the weird variants)
- Clean HTML out of summaries using BeautifulSoup
- Yield RawMention objects

Per-site subclasses just declare:
    name = "Source Display Name"
    category = "media"
    feed_url = "https://example.com/feed/"
    prestige_weight = 0.8

Nothing more is required for a well-behaved RSS feed.

If a feed has quirks (truncated descriptions, weird date formats, HTML
soup in titles), subclasses can override `_extract_excerpt` or
`_extract_title` — but for the five Phase 1 feeds, defaults are enough.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator

import feedparser
from bs4 import BeautifulSoup

from watch_app.http import get as http_get
from watch_app.sources.base import RawMention, Source


logger = logging.getLogger(__name__)


#: Max length for the excerpt we pass to the extractor. Feed summaries vary
#: wildly; capping keeps rows small and extraction fast. Bump later if needed.
MAX_EXCERPT_CHARS = 2000


def _strip_html(raw: str | None) -> str:
    """Return plain text with HTML tags removed and whitespace normalized."""
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    # Collapse runs of whitespace.
    return " ".join(text.split())


def _parse_published(entry: feedparser.FeedParserDict) -> datetime | None:
    """Pull a published datetime out of a feed entry. None if unparseable."""
    # feedparser normalizes most date formats into published_parsed (a time.struct_time).
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class RSSSource(Source):
    """Base class. Subclasses must set name, category, feed_url, prestige_weight."""

    feed_url: str = ""

    def _extract_title(self, entry: feedparser.FeedParserDict) -> str | None:
        raw = entry.get("title", "")
        return _strip_html(raw) or None

    def _extract_excerpt(self, entry: feedparser.FeedParserDict) -> str | None:
        # Try content first (Atom/full-text feeds), then summary.
        content_list = entry.get("content") or []
        if content_list:
            raw = content_list[0].get("value", "")
        else:
            raw = entry.get("summary", "")
        text = _strip_html(raw)
        if not text:
            return None
        return text[:MAX_EXCERPT_CHARS]

    def fetch(self) -> Iterator[RawMention]:
        if not self.feed_url:
            raise RuntimeError(
                f"{self.__class__.__name__}.feed_url is not set"
            )

        try:
            response = http_get(self.feed_url)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to fetch %s feed at %s: %s", self.name, self.feed_url, exc)
            return

        # feedparser accepts bytes or str and is forgiving about malformed
        # feeds. It never raises; it sets .bozo=1 if something was off.
        parsed = feedparser.parse(response.text)
        if parsed.bozo:
            logger.debug(
                "%s feed had parse warnings: %s",
                self.name,
                parsed.get("bozo_exception"),
            )

        entries = parsed.entries or []
        logger.info("%s: fetched %d entries from feed", self.name, len(entries))

        for entry in entries:
            url = (entry.get("link") or "").strip()
            if not url:
                continue

            yield RawMention(
                source_name=self.name,
                url=url,
                title=self._extract_title(entry),
                excerpt=self._extract_excerpt(entry),
                published_at=_parse_published(entry),
                # candidate_names stays empty — the orchestrator fills it via
                # the rule-based (or LLM) extractor.
            )
