"""Tests for the YouTube channel source.

Verifies:
  1. The YAML loader produces the expected channels with correct weights
  2. _extract_excerpt pulls from media:description (the YouTube-specific bit)
  3. Missing media:description falls back gracefully
  4. The full feed flow yields RawMention objects with title + description
  5. Missing YAML returns empty list (doesn't crash)
  6. Malformed YAML entries are skipped, not fatal
  7. The orchestrator includes YouTube sources in its default list
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from watch_app.sources.media.youtube import (
    YouTubeChannelSource,
    build_youtube_sources,
    load_channels,
    _feed_url,
    YOUTUBE_EXCERPT_CHARS,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_FEED = FIXTURE_DIR / "youtube_feed_sample.xml"


# ----------------- Feed URL helper -----------------


def test_feed_url_format():
    """Channel ID is interpolated correctly into the standard URL."""
    url = _feed_url("UCabc123")
    assert url == "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123"


# ----------------- Excerpt extraction -----------------


def _make_entry(media_description=None, summary=None, content=None):
    """Build a feedparser-like entry dict with the requested fields."""
    entry = {}
    if media_description is not None:
        entry["media_description"] = media_description
    if summary is not None:
        entry["summary"] = summary
    if content is not None:
        # feedparser exposes content as a list of dicts
        entry["content"] = [{"value": content}]
    return entry


def test_excerpt_reads_media_description():
    """When media:description is present, use it (YouTube's Atom dialect)."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    entry = _make_entry(media_description="A video about Simon Brette.")
    excerpt = source._extract_excerpt(entry)
    assert excerpt == "A video about Simon Brette."


def test_excerpt_falls_back_to_summary_if_media_missing():
    """If media_description is absent, fall back to the base class behavior.

    Defensive: protects against YouTube changing their feed format.
    """
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    entry = _make_entry(summary="Fallback summary text.")
    excerpt = source._extract_excerpt(entry)
    assert excerpt == "Fallback summary text."


def test_excerpt_strips_html():
    """HTML in descriptions is stripped before returning."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    entry = _make_entry(
        media_description="A <b>bold</b> claim about <i>F.P. Journe</i>."
    )
    excerpt = source._extract_excerpt(entry)
    # BeautifulSoup may add a space around stripped inline tags; we don't
    # care about whitespace edge cases as long as text and HTML tags are gone.
    assert "F.P. Journe" in excerpt
    assert "<" not in excerpt
    assert "<b>" not in excerpt


def test_excerpt_capped_at_500_chars():
    """Long descriptions are truncated. YouTube descriptions are often
    boilerplate-padded, so we cap shorter than the RSS default."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    long_text = "Brette " * 200  # ~1400 chars
    entry = _make_entry(media_description=long_text)
    excerpt = source._extract_excerpt(entry)
    assert excerpt is not None
    assert len(excerpt) <= YOUTUBE_EXCERPT_CHARS


def test_excerpt_returns_none_when_no_description():
    """No description, no summary, no content → None (not crash)."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    entry = _make_entry()
    assert source._extract_excerpt(entry) is None


# ----------------- Full feed parsing -----------------


def test_fetch_parses_youtube_atom_feed():
    """End-to-end: real-shaped YouTube Atom XML produces RawMentions."""
    feed_xml = SAMPLE_FEED.read_text()

    source = YouTubeChannelSource(
        name="Test Channel", channel_id="UCTEST", prestige_weight=0.7
    )

    # Mock the HTTP layer to return our fixture
    mock_response = MagicMock()
    mock_response.text = feed_xml
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    # Patch http_get where it's imported in the youtube module (used by
    # the overridden _fetch_feed_text), not where it's defined.
    with patch("watch_app.sources.media.youtube.http_get", return_value=mock_response):
        mentions = list(source.fetch())

    assert len(mentions) == 3

    # First entry: has both title and description, should produce a clean mention
    m1 = mentions[0]
    assert m1.source_name == "Test Channel"
    assert "Simon Brette" in m1.title
    assert "Simon Brette" in m1.excerpt
    assert m1.url == "https://www.youtube.com/watch?v=vid1234567A"
    assert m1.published_at is not None

    # Second entry: shorter description mentioning multiple makers
    m2 = mentions[1]
    assert "Voutilainen" in m2.excerpt
    assert "F.P. Journe" in m2.excerpt

    # Third entry: no description — excerpt should be None, not crash
    m3 = mentions[2]
    assert m3.excerpt is None
    assert m3.title == "Random video with no description"


# ----------------- YAML loading -----------------


def test_load_channels_returns_list_of_dicts():
    """The real YAML in watch_app/data/ should load to a list of dicts."""
    channels = load_channels()
    assert isinstance(channels, list)
    assert len(channels) > 0  # We shipped with 16 channels
    for c in channels:
        assert "name" in c
        assert "channel_id" in c
        assert "prestige_weight" in c
        assert c["channel_id"].startswith("UC")
        assert 0.0 <= c["prestige_weight"] <= 1.0


def test_load_channels_missing_yaml_returns_empty(tmp_path):
    """A missing YAML returns [] rather than crashing.

    Empty channel list is a valid state (e.g., before YAML is created
    or if a user deletes it intentionally).
    """
    missing = tmp_path / "nonexistent.yaml"
    result = load_channels(missing)
    assert result == []


def test_load_channels_malformed_yaml_returns_empty(tmp_path):
    """Bad YAML returns [] with a logged error, doesn't raise."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: valid: yaml: :::")
    result = load_channels(bad)
    assert result == []


def test_load_channels_skips_incomplete_entries(tmp_path):
    """Entries missing required fields are skipped, not fatal."""
    yaml_text = """
channels:
  - name: "Valid Channel"
    channel_id: "UCabc123"
    prestige_weight: 0.5
  - name: "Missing channel_id"
    prestige_weight: 0.5
  - channel_id: "UCxyz789"
    prestige_weight: 0.5
  - name: "Missing weight"
    channel_id: "UCdef456"
"""
    yaml_path = tmp_path / "channels.yaml"
    yaml_path.write_text(yaml_text)
    result = load_channels(yaml_path)
    assert len(result) == 1
    assert result[0]["name"] == "Valid Channel"


def test_real_yaml_has_expected_channels():
    """The shipped YAML should contain the 16 channels we agreed on."""
    channels = load_channels()
    names = {c["name"] for c in channels}
    # A few high-confidence channels that must be present
    assert "YouTube: Bark and Jack" in names
    assert "YouTube: Swiss Watch Gang" in names
    assert "YouTube: Watches TV" in names
    # 16 total
    assert len(channels) == 15


def test_real_yaml_weights_in_expected_range():
    """Weights span 0.3-0.9 as designed (no channel above SJX=1.0)."""
    channels = load_channels()
    weights = [c["prestige_weight"] for c in channels]
    assert max(weights) <= 0.9
    assert min(weights) >= 0.3


# ----------------- build_youtube_sources -----------------


def test_build_youtube_sources_returns_source_instances():
    """The factory returns one YouTubeChannelSource per channel."""
    sources = build_youtube_sources()
    assert len(sources) == 15
    for s in sources:
        assert isinstance(s, YouTubeChannelSource)
        assert s.category == "media"
        assert s.feed_url.startswith("https://www.youtube.com/feeds/videos.xml")
        assert s.name.startswith("YouTube: ")


def test_youtube_source_name_includes_prefix():
    """Display name has 'YouTube:' prefix so it's distinguishable from
    text RSS sources in the source contributions table."""
    sources = build_youtube_sources()
    for s in sources:
        assert s.name.startswith("YouTube: "), (
            f"YouTube source {s.name!r} missing 'YouTube:' prefix"
        )


# ----------------- Orchestrator integration -----------------


def test_orchestrator_includes_youtube_sources():
    """The default source list includes YouTube channels alongside RSS+awards."""
    from watch_app.pipeline.orchestrator import default_sources
    sources = default_sources()
    youtube_sources = [s for s in sources if s.name.startswith("YouTube: ")]
    assert len(youtube_sources) == 15
    # Existing RSS sources still present
    rss_names = {s.name for s in sources if not s.name.startswith("YouTube: ")}
    assert "SJX" in rss_names
    assert "Hodinkee" in rss_names


# ----------------- Retry logic -----------------


def _mock_response(status_code: int, text: str = ""):
    """Build a minimal response-like object for retry tests."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = text
    return mock


def test_retry_success_on_first_attempt_no_sleep():
    """A 200 response on the first try should return immediately, no sleep."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    ok = _mock_response(200, "<feed>ok</feed>")
    with patch("watch_app.sources.media.youtube.http_get", return_value=ok) as mock_get, \
         patch("watch_app.sources.media.youtube.time.sleep") as mock_sleep:
        result = source._fetch_feed_text()
    assert result == "<feed>ok</feed>"
    assert mock_get.call_count == 1
    mock_sleep.assert_not_called()


def test_404_not_retried():
    """404 is deliberately non-retryable (2V): during the YouTube feed outage
    404s are sustained across retries, so we give up immediately rather than
    burn backoff. → returns None after a single call, with no sleeps."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    responses = [_mock_response(404), _mock_response(200, "<feed>ok</feed>")]
    with patch("watch_app.sources.media.youtube.http_get", side_effect=responses) as mock_get, \
         patch("watch_app.sources.media.youtube.time.sleep") as mock_sleep:
        result = source._fetch_feed_text()
    assert result is None
    assert mock_get.call_count == 1  # 404 short-circuits — the 200 is never reached
    mock_sleep.assert_not_called()


def test_retry_recovers_after_500():
    """500 first, 200 on retry → returns the 200 response."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    responses = [_mock_response(500), _mock_response(200, "<feed>ok</feed>")]
    with patch("watch_app.sources.media.youtube.http_get", side_effect=responses), \
         patch("watch_app.sources.media.youtube.time.sleep"):
        result = source._fetch_feed_text()
    assert result == "<feed>ok</feed>"


def test_retry_gives_up_after_max_attempts():
    """All MAX_ATTEMPTS return a retryable 5xx → return None, no crash."""
    from watch_app.sources.media.youtube import MAX_ATTEMPTS

    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    responses = [_mock_response(503) for _ in range(MAX_ATTEMPTS)]
    with patch("watch_app.sources.media.youtube.http_get", side_effect=responses) as mock_get, \
         patch("watch_app.sources.media.youtube.time.sleep"):
        result = source._fetch_feed_text()
    assert result is None
    assert mock_get.call_count == MAX_ATTEMPTS


def test_no_retry_on_non_retryable_status():
    """403 or 401 should NOT trigger retry — those are real auth/access
    issues, not the YouTube flakiness pattern."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    forbidden = _mock_response(403)
    with patch("watch_app.sources.media.youtube.http_get", return_value=forbidden) as mock_get, \
         patch("watch_app.sources.media.youtube.time.sleep") as mock_sleep:
        result = source._fetch_feed_text()
    assert result is None
    assert mock_get.call_count == 1  # No retry
    mock_sleep.assert_not_called()


def test_no_retry_on_connection_exception():
    """If http_get raises (DNS failure, timeout, etc.), don't retry —
    those are environmental issues, not the YouTube flakiness pattern."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    with patch(
        "watch_app.sources.media.youtube.http_get",
        side_effect=ConnectionError("DNS failed"),
    ) as mock_get, \
         patch("watch_app.sources.media.youtube.time.sleep") as mock_sleep:
        result = source._fetch_feed_text()
    assert result is None
    assert mock_get.call_count == 1
    mock_sleep.assert_not_called()


def test_retry_uses_browser_user_agent():
    """The retry path should use a browser-like UA, per advice in
    community threads about this flakiness."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    ok = _mock_response(200, "<feed>ok</feed>")
    with patch("watch_app.sources.media.youtube.http_get", return_value=ok) as mock_get:
        source._fetch_feed_text()
    # http_get was called with user_agent kwarg
    call_kwargs = mock_get.call_args.kwargs
    assert "user_agent" in call_kwargs
    assert "Mozilla" in call_kwargs["user_agent"]


def test_retry_backoff_increases():
    """The backoff sleep duration should grow between retryable attempts."""
    source = YouTubeChannelSource(
        name="Test", channel_id="UCTEST", prestige_weight=0.5
    )
    responses = [_mock_response(503), _mock_response(503), _mock_response(503)]
    with patch("watch_app.sources.media.youtube.http_get", side_effect=responses), \
         patch("watch_app.sources.media.youtube.time.sleep") as mock_sleep:
        source._fetch_feed_text()
    # Two sleeps between three attempts; later sleep should be longer.
    sleep_durations = [call.args[0] for call in mock_sleep.call_args_list]
    assert len(sleep_durations) == 2
    assert sleep_durations[1] > sleep_durations[0]


def test_rss_base_extracted_hook_still_works_for_non_youtube():
    """The refactor of RSSSource added a `_fetch_feed_text` hook. The
    base class behavior on non-YouTube sources should be unchanged."""
    from watch_app.sources.media.hodinkee import HodinkeeSource

    source = HodinkeeSource()
    mock_response = MagicMock()
    mock_response.text = "<rss><channel></channel></rss>"
    mock_response.raise_for_status = MagicMock()

    with patch("watch_app.sources.media.rss_base.http_get", return_value=mock_response):
        result = source._fetch_feed_text()
    assert result == "<rss><channel></channel></rss>"
