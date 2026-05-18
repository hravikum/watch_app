"""Tests for Reddit subreddit RSS sources.

Verifies:
  1. URL format uses top/week (high-signal subset)
  2. Prestige weights are below all text journalism sources
  3. Fixture-based parsing extracts maker mentions from Reddit Atom XML
  4. Excerpt is populated from <content> (not blank like YouTube's case)
  5. Orchestrator includes all three Reddit sources
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from watch_app.sources.media.reddit import (
    RedditHorologySource,
    RedditWatchesSource,
    RedditWatchmakingSource,
    _top_week_feed,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_FEED = FIXTURE_DIR / "reddit_watchmaking_sample.xml"


# ---------- URL format ----------


def test_top_week_url_format():
    """The helper builds top-of-week URLs (not the default /new feed)."""
    url = _top_week_feed("watchmaking")
    assert url == "https://www.reddit.com/r/watchmaking/top/.rss?sort=top&t=week"


def test_subreddit_names_in_urls():
    """Each source's feed_url contains its subreddit name."""
    assert "watchmaking" in RedditWatchmakingSource.feed_url
    assert "horology" in RedditHorologySource.feed_url
    assert "Watches" in RedditWatchesSource.feed_url


def test_all_subreddits_use_top_week():
    """All three Reddit sources use the top/week filter, not /new."""
    for source_cls in [
        RedditWatchmakingSource,
        RedditHorologySource,
        RedditWatchesSource,
    ]:
        assert "top/.rss" in source_cls.feed_url
        assert "t=week" in source_cls.feed_url


# ---------- Prestige weights ----------


def test_reddit_prestige_below_journalism():
    """Reddit weights are below all text journalism sources.

    Rationale: a Reddit post is a single user's opinion or photo, not
    researched journalism. Should never outrank SJX/Monochrome/Hodinkee.
    Lowest journalism source is Hodinkee at 0.7.
    """
    # All Reddit sources should be at 0.6 or below.
    assert RedditWatchmakingSource.prestige_weight <= 0.6
    assert RedditHorologySource.prestige_weight <= 0.6
    assert RedditWatchesSource.prestige_weight <= 0.6


def test_general_subreddit_lower_than_niche():
    """r/Watches (mainstream, noisy) weighted below r/watchmaking
    (niche, signal-dense)."""
    assert (
        RedditWatchesSource.prestige_weight
        < RedditWatchmakingSource.prestige_weight
    )


def test_categories_set():
    """All Reddit sources categorized as 'media' (RSS-based)."""
    assert RedditWatchmakingSource.category == "media"
    assert RedditHorologySource.category == "media"
    assert RedditWatchesSource.category == "media"


# ---------- Feed parsing ----------


def test_fetch_parses_reddit_atom_feed():
    """End-to-end: real-shaped Reddit Atom XML produces RawMentions
    with titles and excerpts that contain maker names."""
    feed_xml = SAMPLE_FEED.read_text()

    source = RedditWatchmakingSource()
    mock_response = MagicMock()
    mock_response.text = feed_xml
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    with patch(
        "watch_app.sources.media.rss_base.http_get",
        return_value=mock_response,
    ):
        mentions = list(source.fetch())

    assert len(mentions) == 3

    # First entry: self post mentioning Simon Brette
    m1 = mentions[0]
    assert m1.source_name == "r/watchmaking"
    assert "Simon Brette" in m1.title
    assert m1.excerpt is not None
    assert "Simon Brette" in m1.excerpt
    # URL should be the reddit thread URL
    assert "reddit.com/r/watchmaking" in m1.url
    assert m1.published_at is not None

    # Second entry: mentions Hazemann & Monnin
    m2 = mentions[1]
    assert "Hazemann" in m2.title
    assert m2.excerpt is not None
    assert "Hazemann" in m2.excerpt

    # Third entry: link post (less excerpt content but still a mention)
    m3 = mentions[2]
    assert "Voutilainen" in m3.title


def test_excerpt_strips_reddit_html_chrome():
    """Reddit wraps content in tables, [link]/[comments] anchors, etc.
    The excerpt should strip all of that and surface just the readable
    post body. Critical because the resolver matches on text — if the
    excerpt is mostly chrome, maker name matching fails."""
    feed_xml = SAMPLE_FEED.read_text()
    source = RedditWatchmakingSource()
    mock_response = MagicMock()
    mock_response.text = feed_xml
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    with patch(
        "watch_app.sources.media.rss_base.http_get",
        return_value=mock_response,
    ):
        mentions = list(source.fetch())

    # Check no HTML tags survived in any excerpt
    for m in mentions:
        if m.excerpt:
            assert "<" not in m.excerpt
            assert ">" not in m.excerpt


# ---------- Orchestrator integration ----------


def test_orchestrator_includes_reddit_sources():
    """Default source list includes all three Reddit sources."""
    from watch_app.pipeline.orchestrator import default_sources
    sources = default_sources()
    names = {s.name for s in sources}
    assert "r/watchmaking" in names
    assert "r/horology" in names
    assert "r/Watches" in names


def test_orchestrator_order_reddit_before_youtube():
    """Sources run in a deterministic order. Reddit sources should come
    after RSS+awards but before YouTube — the existing pattern groups
    higher-prestige sources first."""
    from watch_app.pipeline.orchestrator import default_sources
    sources = default_sources()
    names = [s.name for s in sources]
    # Find indices
    reddit_idx = next(i for i, n in enumerate(names) if n == "r/watchmaking")
    youtube_first = next(
        i for i, n in enumerate(names) if n.startswith("YouTube: ")
    )
    assert reddit_idx < youtube_first, (
        "Reddit sources should appear before YouTube sources in default list"
    )
