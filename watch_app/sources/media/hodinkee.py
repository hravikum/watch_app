"""Hodinkee RSS source.

The largest watch enthusiast publication. Covers everything from indies
to mainstream luxury, so the signal per article is lower than SJX or
Monochrome — but the volume and reach are much higher.

Weighted accordingly (0.7 vs 1.0 for SJX).
"""
from __future__ import annotations

from watch_app.sources.media.rss_base import RSSSource


class HodinkeeSource(RSSSource):
    name = "Hodinkee"
    category = "media"
    feed_url = "https://www.hodinkee.com/articles/rss.xml"
    prestige_weight = 0.95  # 4A: most first-surfaced (37) + top-tier yield; a peer of SJX/Monochrome
