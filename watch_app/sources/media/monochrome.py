"""Monochrome Watches RSS source.

Monochrome is an indie-focused publication. Frank Geelen (founder) sits on
the Louis Vuitton Watch Prize jury, so their coverage is a strong positive
signal.

Feed verified as live and returning application/rss+xml.
"""
from __future__ import annotations

from watch_app.sources.media.rss_base import RSSSource


class MonochromeSource(RSSSource):
    name = "Monochrome Watches"
    category = "media"
    feed_url = "https://monochrome-watches.com/blog/feed/"
    prestige_weight = 0.9
