"""Quill & Pad RSS source.

Publication founded by Ian Skellern and Elizabeth Doerr. Particular strength
in coverage of independent watchmakers, artistic crafts, and complex
complications. Good complement to Monochrome and SJX.

Standard WordPress RSS feed.
"""
from __future__ import annotations

from watch_app.sources.media.rss_base import RSSSource


class QuillAndPadSource(RSSSource):
    name = "Quill & Pad"
    category = "media"
    feed_url = "https://quillandpad.com/feed/"
    prestige_weight = 0.8
