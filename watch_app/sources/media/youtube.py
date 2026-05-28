"""YouTube channel RSS source.

Every YouTube channel has a hidden Atom feed at:
    https://www.youtube.com/feeds/videos.xml?channel_id=<UC...>

It's Atom 1.0 with one wrinkle: the description text lives in
`<media:description>` inside `<media:group>`, not in the standard
`<summary>` or `<content>` elements. feedparser exposes the namespaced
field as `entry.media_description` (lowercased + underscored).

We override `_extract_excerpt` to read from that field instead.
Everything else (title, URL, publish date) follows the standard Atom
shape that the base class handles.

## Known issue: YouTube feed endpoint flakiness

As of 2025-2026, YouTube's feed endpoint intermittently returns 404
and 500 errors for valid, active channel IDs. The same channel works
fine in a browser at the same moment. Other RSS readers (miniflux,
RSS-Bridge, n8n) report the same pattern — see:
  https://discuss.ai.google.dev/t/youtube-rss-feed-endpoint-returns-404-errors/113379
  https://community.n8n.io/t/youtube-rss-feed-endpoint-returns-404-errors/241692

The pattern in the n8n thread is that failures cluster in specific
UTC hours (09:00-12:00 reported), though our own observation suggests
the pattern isn't strictly time-bound.

Mitigation: we override `_fetch_feed_text` to retry on transient 5xx
errors with short backoff. This catches single-request glitches without
adding meaningful latency on success. We do NOT retry 404s: during the
ongoing outage they are sustained (never recover within a scan), so
retrying them only burns backoff time. Retries inside the same scan
won't help if we're inside a longer outage window — those days the
channel just won't contribute. Cumulative data over weeks is the right
unit of measurement, not per-scan reliability.

## Channels

Channel definitions live in `watch_app/data/youtube_channels.yaml` so
the channel list is data, not code. To add a channel, append a row to
the YAML. No new Python files needed.

## Prestige weighting

Channels are weighted on the same 0.0-1.0 scale as the existing RSS
sources (SJX=1.0, Monochrome=0.9, Hodinkee=0.7). Even our highest
YouTube channel weight is 0.9 — never exceeding our strongest text
sources, because video is supplementary to journalism, not a
replacement.

## Description length cap

YouTube descriptions can be very long and are often padded with
boilerplate (sponsor blurbs, affiliate links, social media handles,
timestamps). We cap excerpt at 500 chars rather than the base class's
2000 — the maker name, if mentioned, is almost always in the first
few sentences.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import feedparser
import yaml

from watch_app.http import get as http_get
from watch_app.sources.media.rss_base import RSSSource, _strip_html


logger = logging.getLogger(__name__)


#: Where the channel list lives, parallel to known_makers.yaml.
CHANNELS_YAML = (
    Path(__file__).resolve().parent.parent.parent / "data" / "youtube_channels.yaml"
)

#: YouTube descriptions are often boilerplate-heavy. Tighter cap than RSS.
YOUTUBE_EXCERPT_CHARS = 500

#: HTTP status codes worth retrying. 5xx are conventional transient
#: server errors. 404 is deliberately excluded: during the 2025-2026
#: YouTube feed outage, 404s are sustained across all retries within a
#: scan (never observed succeeding on retry), so retrying a 404 only
#: wastes backoff time. A 404 that genuinely is a one-request glitch
#: will be picked up on the next scheduled scan.
RETRYABLE_STATUS_CODES = (500, 502, 503, 504)

#: Number of attempts including the initial one. So MAX_ATTEMPTS=3 means
#: 1 initial + 2 retries.
MAX_ATTEMPTS = 3

#: Browser-like UA. Other tools report this doesn't fix the flakiness, but
#: it's a cheap thing to try and may help in some cases.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


class YouTubeChannelSource(RSSSource):
    """One subclass instance per channel, parameterized at construction time.

    Differs from a normal RSSSource in three ways:
      1. name, feed_url, and prestige_weight come from YAML, not class attributes
      2. _extract_excerpt reads media:description (YouTube's Atom dialect)
      3. _fetch_feed_text retries on YouTube's known-flaky error codes
    """

    category = "media"

    def __init__(self, *, name: str, channel_id: str, prestige_weight: float):
        # Set instance attributes — RSSSource normally reads these as class
        # attributes but Python falls back to instance attributes too.
        self.name = name
        self.channel_id = channel_id
        self.feed_url = _feed_url(channel_id)
        self.prestige_weight = prestige_weight

    def _extract_excerpt(self, entry: feedparser.FeedParserDict) -> str | None:
        # YouTube puts the description in <media:group><media:description>.
        # feedparser flattens this to entry.media_description.
        # Fall back to the base behavior if that field is missing (defensive
        # in case YouTube ever changes its feed format).
        raw = entry.get("media_description")
        if raw:
            text = _strip_html(raw)
            if text:
                return text[:YOUTUBE_EXCERPT_CHARS]
        return super()._extract_excerpt(entry)

    def _fetch_feed_text(self) -> str | None:
        """Fetch the feed with retry on transient 5xx errors.

        YouTube returns 404 and 500 intermittently for valid, active
        channel IDs. We retry 5xx up to MAX_ATTEMPTS times with short
        backoff, but NOT 404 — during the ongoing outage 404s are
        sustained across retries, so retrying them only wastes time.

        On non-retryable errors (e.g. 404, true 403, real connection
        failure), we log and give up immediately rather than waste time.
        The scan continues with other sources.
        """
        last_status: int | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                # Use a browser-like UA on YouTube specifically. Per the n8n
                # thread, doesn't always help, but it's free.
                response = http_get(self.feed_url, user_agent=BROWSER_UA)
            except Exception as exc:
                # Connection-level failure (DNS, timeout, etc.). Not a
                # YouTube flakiness pattern; bail.
                logger.warning(
                    "Failed to fetch %s feed at %s: %s",
                    self.name, self.feed_url, exc,
                )
                return None

            if response.status_code < 400:
                if attempt > 1:
                    logger.info(
                        "%s feed succeeded on retry %d/%d",
                        self.name, attempt, MAX_ATTEMPTS,
                    )
                return response.text

            last_status = response.status_code
            if response.status_code not in RETRYABLE_STATUS_CODES:
                # Not the YouTube flakiness pattern (e.g. true 403).
                logger.warning(
                    "Failed to fetch %s feed at %s: HTTP %d (not retryable)",
                    self.name, self.feed_url, response.status_code,
                )
                return None

            if attempt < MAX_ATTEMPTS:
                # Exponential-ish backoff: 1s, 3s.
                backoff = float(attempt * 2 - 1)
                logger.info(
                    "%s feed returned HTTP %d (attempt %d/%d), retrying in %.1fs",
                    self.name, response.status_code, attempt, MAX_ATTEMPTS, backoff,
                )
                time.sleep(backoff)

        logger.warning(
            "Failed to fetch %s feed at %s after %d attempts (final status: %s)",
            self.name, self.feed_url, MAX_ATTEMPTS, last_status,
        )
        return None


def load_channels(yaml_path: Path = CHANNELS_YAML) -> list[dict[str, Any]]:
    """Read the channel YAML and return a list of dicts.

    Each dict has keys: name, channel_id, prestige_weight, tier.
    The `tier` field is informational only (used in the chunk notes /
    documentation); not consumed by the scoring code, which only uses
    the weight.

    Missing or malformed YAML returns an empty list rather than raising.
    A scan with zero YouTube channels is a valid state (e.g., before the
    YAML is created).
    """
    if not yaml_path.exists():
        logger.warning("youtube channels YAML missing at %s", yaml_path)
        return []
    try:
        data = yaml.safe_load(yaml_path.read_text())
    except yaml.YAMLError as exc:
        logger.error("failed to parse %s: %s", yaml_path, exc)
        return []
    channels = (data or {}).get("channels") or []
    out = []
    for entry in channels:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        channel_id = entry.get("channel_id")
        weight = entry.get("prestige_weight")
        if not name or not channel_id or weight is None:
            logger.warning("skipping incomplete YouTube channel entry: %r", entry)
            continue
        out.append(
            {
                "name": name,
                "channel_id": channel_id,
                "prestige_weight": float(weight),
                "tier": entry.get("tier"),
            }
        )
    return out


def build_youtube_sources() -> list[YouTubeChannelSource]:
    """Instantiate one YouTubeChannelSource per channel in the YAML.

    Called by the orchestrator's default source list. Returns [] if the
    YAML is missing or empty — the scan continues with non-YouTube sources.
    """
    return [
        YouTubeChannelSource(
            name=c["name"],
            channel_id=c["channel_id"],
            prestige_weight=c["prestige_weight"],
        )
        for c in load_channels()
    ]
