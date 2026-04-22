"""SJX (Watches by SJX) RSS source.

Jiaxian Su's publication from Singapore. Jiaxian was on the inaugural LV
Watch Prize jury. SJX has probably the highest signal-to-noise ratio in
watch journalism specifically for independent watchmaking.

Feed is a standard WordPress RSS feed.
"""
from __future__ import annotations

from watch_app.sources.media.rss_base import RSSSource


class SJXSource(RSSSource):
    name = "SJX"
    category = "media"
    feed_url = "https://watchesbysjx.com/feed/"
    prestige_weight = 1.0
