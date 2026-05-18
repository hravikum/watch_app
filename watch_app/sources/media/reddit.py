"""Reddit subreddit RSS sources.

Reddit exposes Atom feeds for every subreddit without authentication:
    https://www.reddit.com/r/<subreddit>/.rss
    https://www.reddit.com/r/<subreddit>/top/.rss?sort=top&t=week

We use the `top/week` form rather than the default "new" feed. The
new feed includes all posts including low-quality and off-topic ones;
top/week filters to what the community actually upvoted, which is far
more likely to mention specific makers worth tracking.

## Subreddit choices

Three subreddits cover the relevant ground without overlap noise:

- **r/watchmaking** (prestige 0.6) — independent watchmakers, signal-
  dense for our purposes. Discussions of specific makers, watchmaking
  technique, indie releases.
- **r/horology** (prestige 0.6) — broader watchmaking discussion,
  history, mechanical horology. Some overlap with r/watchmaking but
  a different audience reach.
- **r/Watches** (prestige 0.4) — the main watch subreddit, large
  community, much higher volume but more noise (mainstream brands,
  collection photos, "what watch is this" posts). Lower prestige
  reflects the lower signal-to-volume ratio.

Skipped: r/microbrand (largely affordable enthusiast brands not in
our seed target), r/WatchesCirclejerk (humor/satire — may surface
makers but the framing makes title parsing fragile).

## Prestige weights

Reddit posts are community-curated discussion, not journalism. The
weights are intentionally below all of our text journalism sources
(SJX=1.0, Monochrome=0.9, Hodinkee=0.7) because a Reddit post is
typically a single user's opinion or photo, not a researched piece.

## Format details

Reddit returns Atom 1.0. The default RSSSource._extract_excerpt
handles the `<content>` field correctly — Reddit puts the post body
(for self posts) or a "[link]" placeholder (for link posts) there.
No custom override needed.

## Rate limits

Reddit's unauthenticated RSS limit is ~100 requests per 10 minutes
per IP. We make 3 requests per scan (3 subreddits) once daily.
Nowhere near the limit. The existing http_get rate limiting (1.5s
between same-host requests) keeps us polite.
"""
from __future__ import annotations

from watch_app.sources.media.rss_base import RSSSource


def _top_week_feed(subreddit: str) -> str:
    """Build a Reddit top-of-week RSS URL for a subreddit.

    The top/week query returns the highest-upvoted posts from the
    past 7 days, which gives us a steady signal that doesn't churn
    on individual low-quality posts. The `t=week` window matches
    the cadence of weekly content review on the consuming side
    (/movers default is 7 days; this aligns).
    """
    return f"https://www.reddit.com/r/{subreddit}/top/.rss?sort=top&t=week"


class RedditWatchmakingSource(RSSSource):
    name = "r/watchmaking"
    category = "media"
    feed_url = _top_week_feed("watchmaking")
    prestige_weight = 0.6


class RedditHorologySource(RSSSource):
    name = "r/horology"
    category = "media"
    feed_url = _top_week_feed("horology")
    prestige_weight = 0.6


class RedditWatchesSource(RSSSource):
    name = "r/Watches"
    category = "media"
    feed_url = _top_week_feed("Watches")
    prestige_weight = 0.4
