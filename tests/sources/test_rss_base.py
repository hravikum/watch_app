"""Tests for the generic RSS source.

Uses a hand-rolled RSS 2.0 fixture so tests are hermetic (no network, no
recorded real-site HTML to go stale). This exercises the feedparser
integration, HTML stripping, date parsing, and URL extraction.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from watch_app.sources.media.rss_base import RSSSource


class _TestRSSSource(RSSSource):
    """Concrete test subclass — only exists so we can instantiate the ABC."""

    name = "TestFeed"
    category = "media"
    feed_url = "https://example.com/feed/"
    prestige_weight = 0.5


#: A synthetic RSS feed covering the edge cases we care about.
SAMPLE_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Watch Blog</title>
    <link>https://example.com/</link>
    <description>Watch news</description>
    <item>
      <title>Simon Brette releases a new chronometer</title>
      <link>https://example.com/simon-brette-new</link>
      <pubDate>Mon, 15 Mar 2026 10:00:00 +0000</pubDate>
      <description>&lt;p&gt;The French independent watchmaker &lt;strong&gt;Simon Brette&lt;/strong&gt; has unveiled a stunning new piece.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Interview: &amp; Voutilainen on craftsmanship</title>
      <link>https://example.com/voutilainen-interview</link>
      <pubDate>Fri, 12 Mar 2026 14:30:00 +0000</pubDate>
      <description>Kari Voutilainen discusses traditional finishing.</description>
    </item>
    <item>
      <title>Article with no date</title>
      <link>https://example.com/no-date</link>
      <description>Text content.</description>
    </item>
    <item>
      <title>Broken: no link here</title>
      <description>This entry has no link and should be skipped.</description>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
def mocked_feed():
    """Patch http.get so the RSS source sees our fixture instead of the network."""
    response = MagicMock()
    response.status_code = 200
    response.text = SAMPLE_FEED_XML
    response.raise_for_status = MagicMock()
    with patch("watch_app.sources.media.rss_base.http_get", return_value=response) as m:
        yield m


def test_yields_mentions_with_correct_fields(mocked_feed):
    source = _TestRSSSource()
    mentions = list(source.fetch())

    # 4 items in the feed but one has no link — dropped.
    assert len(mentions) == 3

    first = mentions[0]
    assert first.source_name == "TestFeed"
    assert first.url == "https://example.com/simon-brette-new"
    assert first.title == "Simon Brette releases a new chronometer"
    # HTML stripped from description.
    assert "Simon Brette" in first.excerpt
    assert "<strong>" not in first.excerpt
    assert "<p>" not in first.excerpt


def test_parses_published_date(mocked_feed):
    source = _TestRSSSource()
    mentions = list(source.fetch())

    first = mentions[0]
    assert first.published_at is not None
    assert first.published_at == datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)


def test_handles_missing_date(mocked_feed):
    source = _TestRSSSource()
    mentions = list(source.fetch())

    # Third mention has no pubDate.
    no_date = [m for m in mentions if m.url.endswith("no-date")][0]
    assert no_date.published_at is None


def test_handles_html_entities_in_title(mocked_feed):
    source = _TestRSSSource()
    mentions = list(source.fetch())

    interview = [m for m in mentions if "voutilainen-interview" in m.url][0]
    # feedparser should decode &amp; to &.
    assert "&" in interview.title
    assert "&amp;" not in interview.title


def test_skips_entries_with_no_link(mocked_feed):
    source = _TestRSSSource()
    mentions = list(source.fetch())

    # The "Broken: no link here" entry should not appear.
    assert not any("Broken" in (m.title or "") for m in mentions)


def test_candidate_names_starts_empty(mocked_feed):
    """RSS source doesn't pre-populate candidates — orchestrator does that."""
    source = _TestRSSSource()
    mentions = list(source.fetch())
    for m in mentions:
        assert m.candidate_names == []


def test_empty_feed_url_raises():
    class _NoUrl(RSSSource):
        name = "NoUrl"
        category = "media"
        feed_url = ""

    with pytest.raises(RuntimeError, match="feed_url is not set"):
        list(_NoUrl().fetch())


def test_fetch_failure_returns_empty(mocked_feed):
    """If HTTP fails, we log and yield nothing — don't crash the whole scan."""
    with patch(
        "watch_app.sources.media.rss_base.http_get",
        side_effect=RuntimeError("network down"),
    ):
        mentions = list(_TestRSSSource().fetch())
    assert mentions == []
