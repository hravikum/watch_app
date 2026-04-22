"""Quick diagnostic: check that all five RSS feeds are reachable and parseable.

Run from the project root:

    python scripts/check_feeds.py

Output: one line per feed with status, entry count, and the title of the
most recent entry. If any feed shows 0 entries or an error, tell me which
one and we'll debug.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make watch_app importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402

from watch_app.sources.media.fratello import FratelloSource  # noqa: E402
from watch_app.sources.media.hodinkee import HodinkeeSource  # noqa: E402
from watch_app.sources.media.monochrome import MonochromeSource  # noqa: E402
from watch_app.sources.media.quill_and_pad import QuillAndPadSource  # noqa: E402
from watch_app.sources.media.sjx import SJXSource  # noqa: E402


SOURCES = [
    SJXSource(),
    MonochromeSource(),
    FratelloSource(),
    HodinkeeSource(),
    QuillAndPadSource(),
]


def main() -> int:
    problems = 0
    for source in SOURCES:
        try:
            import httpx

            r = httpx.get(
                source.feed_url,
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": "watch-app/0.1 diagnostic"},
            )
            parsed = feedparser.parse(r.text)
            n = len(parsed.entries or [])
            latest = parsed.entries[0].get("title", "(no title)") if n else "—"
            status = "OK" if r.status_code == 200 and n > 0 else "FAIL"
            if status == "FAIL":
                problems += 1
            print(
                f"  {status:<4} {source.name:<20} "
                f"HTTP {r.status_code}  {n:>3} entries  "
                f"latest: {latest[:60]}"
            )
        except Exception as exc:
            problems += 1
            print(f"  FAIL {source.name:<20} exception: {exc}")

    print()
    if problems:
        print(f"{problems} feed(s) had issues. Tell me which ones.")
        return 1
    print("All feeds reachable and parseable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
