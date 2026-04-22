"""End-to-end test: orchestrator runs a (mocked) RSS source, extracts maker
names from article content, resolves to canonical Makers, writes Mentions."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from watch_app.pipeline.orchestrator import run_scan
from watch_app.sources.media.rss_base import RSSSource
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention


class _FakeRSS(RSSSource):
    name = "Fake Feed"
    category = "media"
    feed_url = "https://fake.example.com/feed/"
    prestige_weight = 1.0


FEED_XML_WITH_REAL_MAKERS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Fake</title>
  <link>https://fake.example.com/</link>
  <description>test</description>
  <item>
    <title>Simon Brette's chronometer wins acclaim</title>
    <link>https://fake.example.com/a1</link>
    <pubDate>Mon, 15 Mar 2026 10:00:00 +0000</pubDate>
    <description>The French watchmaker Simon Brette continues to impress.</description>
  </item>
  <item>
    <title>Petermann Bédat and Kari Voutilainen discuss finishing</title>
    <link>https://fake.example.com/a2</link>
    <pubDate>Fri, 12 Mar 2026 14:30:00 +0000</pubDate>
    <description>Two indie masters on their craft.</description>
  </item>
  <item>
    <title>Some unrelated rolex thing</title>
    <link>https://fake.example.com/a3</link>
    <pubDate>Fri, 12 Mar 2026 14:30:00 +0000</pubDate>
    <description>Rolex news nobody here cares about.</description>
  </item>
</channel></rss>
"""


def test_rss_pipeline_creates_mentions_for_seed_makers(test_engine):
    response = MagicMock()
    response.status_code = 200
    response.text = FEED_XML_WITH_REAL_MAKERS
    response.raise_for_status = MagicMock()

    with patch(
        "watch_app.sources.media.rss_base.http_get", return_value=response
    ):
        stats = run_scan(sources=[_FakeRSS()], use_llm=False)

    # Seed makers were loaded; rule-based extractor found at least Simon Brette,
    # Petermann Bédat, Kari Voutilainen, and (since we seed Rolex) Rolex too.
    assert stats["mentions_written"] >= 4

    with session_scope() as session:
        # Look up canonical Makers and confirm they have mentions from this source.
        simon = (
            session.query(Maker)
            .filter(Maker.canonical_name == "Simon Brette")
            .one()
        )
        assert len(simon.mentions) >= 1
        assert any("fake.example.com/a1" in m.url for m in simon.mentions)

        petermann = (
            session.query(Maker)
            .filter(Maker.canonical_name == "Petermann Bédat")
            .one()
        )
        assert any("fake.example.com/a2" in m.url for m in petermann.mentions)

        # "Voutilainen" in text should resolve to Kari Voutilainen (alias).
        voutilainen = (
            session.query(Maker)
            .filter(Maker.canonical_name == "Kari Voutilainen")
            .one()
        )
        assert any("fake.example.com/a2" in m.url for m in voutilainen.mentions)


def test_rss_pipeline_is_idempotent(test_engine):
    """Second run with same feed content should produce no new mentions."""
    response = MagicMock()
    response.status_code = 200
    response.text = FEED_XML_WITH_REAL_MAKERS
    response.raise_for_status = MagicMock()

    with patch(
        "watch_app.sources.media.rss_base.http_get", return_value=response
    ):
        run_scan(sources=[_FakeRSS()], use_llm=False)
        with session_scope() as session:
            first_count = session.query(Mention).count()

        stats2 = run_scan(sources=[_FakeRSS()], use_llm=False)
        with session_scope() as session:
            second_count = session.query(Mention).count()

    assert first_count == second_count
    assert stats2["mentions_written"] == 0
