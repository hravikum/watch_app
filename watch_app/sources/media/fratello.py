"""Fratello Watches RSS source.

Dutch publication with broad coverage. Two Fratello staff sit on the LV
Prize committee of experts, so their editorial eye aligns with indie
discovery goals.

Feed is a standard WordPress RSS feed.
"""
from __future__ import annotations

from watch_app.sources.media.rss_base import RSSSource


class FratelloSource(RSSSource):
    name = "Fratello Watches"
    category = "media"
    feed_url = "https://www.fratellowatches.com/feed/"
    prestige_weight = 0.7
