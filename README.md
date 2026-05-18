# Watch App

A local tool for discovering up-and-coming independent watchmakers — the kind of
makers who might become nominees, finalists, or winners of prizes like the Louis
Vuitton Watch Prize for Independent Creatives.

The app ingests from watch media and award nominations, resolves mentions against
a curated seed of ~125 known makers (plus auto-creates new ones it sees), scores
each maker under two rankings, and surfaces results in a CLI and local web UI.

## What it does today

- **Ingests from ~26 sources daily**: 5 watch publications via RSS (SJX,
  Monochrome, Fratello, Hodinkee, Quill & Pad), 3 GPHG editions via a
  scraper of the official site (2023/2024/2025), 3 Reddit subreddits
  (r/watchmaking, r/horology, r/Watches), and 15 YouTube channels
  covering indie watchmakers across three prestige tiers.
- **Tracks 125 curated seed makers** plus anyone new that surfaces from GPHG or
  an article mentioning them. The seed includes every LV Watch Prize finalist,
  every AHCI member, most GPHG regulars, and major conglomerate brands flagged
  as non-independent.
- **Ranks under two presets**:
  - `rising_star` — independents gaining traction across multiple outlets
  - `hidden_gem` — makers with minimal current coverage
- **Runs on a daily schedule** via macOS launchd, so data accumulates without
  you doing anything.
- **Shows scores, signals, and the full mention history** per maker in a
  clickable local web UI, or a Rich-formatted CLI.
- **Explains how every rank was computed** on a `/methodology` page —
  signal definitions, preset weights (pulled live from code), per-source
  contribution stats (mentions, distinct makers, makers first-surfaced),
  and a worked example for each preset that walks through the top-ranked
  maker's math end-to-end.
- **Catalogs GPHG-nominated watches per maker.** Each maker's detail page
  shows the watches they've had nominated for any GPHG edition — name, year,
  case material and diameter, movement type, power reserve, water resistance,
  price, limited edition count, image. Scoped intentionally to GPHG nominees
  so the data quality stays high (every spec field has a verified source URL).
- **Compares makers across both presets** at `/comparison`. One sortable
  table with every maker's `rising_star` rank, `hidden_gem` rank, and the
  divergence between them. Sorting by divergence surfaces makers the two
  presets disagree most about — usually the most informative population to
  investigate.
- **Tracks rank changes over time** at `/movers`. Climbers, fallers, and
  new entrants over a configurable lookback window (3, 7, or 14 days)
  under either preset. The "rising star" preset's purpose is finally
  visible as actual movement, not a static snapshot.
- **Links to maker websites** on every detail page where one exists. 117
  of 125 seed makers have a verified website URL — the remaining 8 (e.g.
  Philippe Dufour) genuinely don't have public web presence.

## Setup

Requires Python 3.12 (some dependencies don't yet support 3.14).

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Optional: add an Anthropic API key for LLM-backed extraction.
# Rule-based extraction works fine without this.
cp .env.example .env

make setup-db    # Creates DB and applies migrations
make scan        # First scan — fetches all sources, ~30-60 seconds cold cache
make top         # See top 20 makers under rising_star
make serve       # Web UI at http://localhost:8000
```

## Daily scheduling

To make scans run automatically every morning:

```bash
python -m watch_app.cli schedule
```

This prints a macOS LaunchAgent plist and the three `launchctl` commands to
install it. Follow the steps; the scan will run at 7am local time every day,
catching up automatically if your Mac was asleep. Unlike cron, launchd handles
sleep/wake correctly on a laptop.

To disable later:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.watchapp.scan.plist
```

Check run history any time:
```bash
python -m watch_app.cli history
```

## CLI reference

```bash
python -m watch_app.cli scan                    # Run all sources + rescore
python -m watch_app.cli scan --use-llm          # Use Claude for extraction (costs API credits)
python -m watch_app.cli scan --no-normalize     # Raw-weight scoring (debugging)
python -m watch_app.cli scan --trigger scheduled  # Used by launchd

python -m watch_app.cli top                     # Top 20 under rising_star
python -m watch_app.cli top --preset hidden_gem
python -m watch_app.cli top --limit 50

python -m watch_app.cli maker "Simon Brette"    # Details + full mention list
python -m watch_app.cli history --limit 30      # Recent scan runs
python -m watch_app.cli schedule                # Print launchd setup

python -m watch_app.cli serve                   # Web UI at http://localhost:8000
```

Makefile shortcuts are thin wrappers: `make scan`, `make top`, `make history`,
`make serve`, `make test`.

## How scoring works

Each maker gets 7 signals computed:

| Signal | What it measures |
|---|---|
| `mention_count` | Total mentions across all sources |
| `prestige_weighted_mentions` | Mentions weighted by source prestige. Award sources (GPHG editions) capped at one contribution per source — a brand with 14 nominee watches doesn't get 14× the prestige of one with 1. Media unbounded. |
| `source_diversity` | Distinct source count — is it broad coverage or one outlet? |
| `velocity_90d` | Mentions in the last 90 days — currently accelerating? |
| `first_seen_recency` | How recently the maker's first mention was (linear decay over 180 days) |
| `independent_flag` | 1 if the maker is flagged independent, 0 otherwise |
| `obscurity` | `1 / (1 + mention_count)` — peaks at zero mentions |

Signals are z-score normalized by default (each has mean 0, stddev 1) so the
preset weights compose cleanly. The two presets:

- **`rising_star`** weights: source_diversity, prestige_weighted_mentions,
  first_seen_recency, and velocity_90d heavily. Multiplied by independent_flag,
  which zeros out conglomerate brands regardless of other signals.
- **`hidden_gem`** weights obscurity almost exclusively. Caps eligibility at 2
  mentions — anyone with 3+ mentions is filtered out. Prestige and recency act
  as tiny tie-breakers within the zero-mention tail.

## The methodology page

Open `http://localhost:8000/methodology` to see the full scoring model rendered
from live code. The page has five sections:

- **Signals** — each of the 7 signals with its formula and a plain-English
  description of what it captures
- **Normalization** — why z-scoring is necessary and how it affects the way
  weights compose
- **Presets** — the weights for each preset, pulled directly from
  `watch_app/scoring/presets.py`, plus eligibility gates (the conglomerate
  multiplier for `rising_star`, the mention cap for `hidden_gem`)
- **Source Contributions** — per-source statistics: total mentions, distinct
  makers covered, makers first-surfaced (the source-quality signal), and
  recent activity. Helps answer "is this source pulling its weight?" and
  surfaces feeds that have gone silent.
- **Worked Examples** — for each preset, the currently top-ranked maker is
  walked through end-to-end: raw signal values, z-normalized values, preset
  weight, and per-signal contribution summing to their final composite score

The page updates automatically when preset weights change in code. A
regression test (`test_worked_example_final_score_matches_ranker`) asserts
that the math shown on the page matches the math the ranker actually used —
to within 1e-9 — so the page cannot silently lie if the ranker gets refactored.

The same page is linked as "How is this scored? →" next to every maker's
signal breakdown on their detail page, so you can always find your way
back to the explanation.

## How sources work

Each source is a Python class that yields `RawMention` objects. Sources fall into
three shapes:

- **RSS-based media sources** (`sources/media/*.py`) — inherit from `RSSSource`,
  specify a feed URL and prestige weight. Title and article text go through the
  rule-based extractor to find maker mentions against the 125-maker vocabulary.
- **Structured award sources** (`sources/awards/*.py`) — the GPHG scraper parses
  the nominee list directly from the official Next.js page. It doesn't use the
  general extractor because GPHG's data is already well-structured (brand name
  per watch, no prose to parse).
- **Reddit subreddits** (`sources/media/reddit.py`) — RSS-based, but use the
  `top/.rss?sort=top&t=week` URL form rather than the default "new" feed.
  This filters to community-upvoted posts which are far more likely to
  mention specific makers worth tracking. Prestige weights below all
  journalism sources (0.4-0.6) because Reddit posts are single-user
  opinions or photos, not researched pieces.
- **YouTube channels** (`sources/media/youtube.py`) — also RSS-based but
  data-driven from `watch_app/data/youtube_channels.yaml`. One
  `YouTubeChannelSource` per channel, with a custom `_fetch_feed_text` that
  retries on YouTube's known-flaky feed endpoint (404/500 intermittent on
  valid channel IDs) and a custom `_extract_excerpt` that reads
  `media:description` from YouTube's Atom dialect. Adding a channel is a
  one-line YAML edit, no code.

New maker names encountered by any source are auto-created with sensible defaults
(is_independent=True). The seed audit regression test (`tests/test_seed_audit.py`)
locks in the invariant that 34 known conglomerates must stay flagged as
non-independent — catches the recurring mistake of Hublot/Chaumet/etc. polluting
rising_star.

## Project layout

```
watch_app/
├── sources/              # One module per source
│   ├── media/            # RSS-based: SJX, Monochrome, Fratello, Hodinkee, Quill & Pad,
│   │                     # reddit.py (3 subreddits), plus youtube.py (data-driven
│   │                     # from youtube_channels.yaml)
│   ├── awards/           # GPHG list scraper (gphg.py) + per-watch detail parser (gphg_detail.py)
│   ├── dummy.py          # Test source for unit tests
│   └── base.py           # Source / RawMention protocol
├── extraction/           # Rule-based entity extraction; resolver.py fuzzy-matches to Makers
├── llm/                  # Anthropic client + content-hash response cache
├── scoring/              # signals.py computes the 7 signals; ranker.py composes them under presets;
│                         # methodology.py builds worked examples and source-contribution stats;
│                         # queries.py has fetch_top_makers, fetch_all_makers_comparison,
│                         # and fetch_rank_changes used by the web UI
├── storage/              # SQLAlchemy models (Maker, Mention, Signal, Score, ScanRun, Source, WatchModel)
├── pipeline/             # orchestrator.py wires the layers: fetch → extract → resolve → store →
│                         # fetch GPHG model details → score
├── api/                  # FastAPI routers; makers.py has dashboard, detail, methodology,
│                         # comparison, and movers routes
├── web/templates/        # Jinja2 + HTMX (dashboard.html, maker_detail.html, methodology.html,
│                         # comparison.html, movers.html, _makers_table.html)
├── data/
│   ├── known_makers.yaml      # The 125-maker curated seed (117 with website URLs)
│   └── youtube_channels.yaml  # 15 YouTube channels with prestige weights and tier annotations
└── cli.py                # Typer CLI entry point

tests/                    # 279 tests; run with `make test`
alembic/                  # Schema migrations
```

## Architecture notes

- **SQLite** is used as the store — simple, good enough at this scale, no daemon.
  Scales fine up to low millions of rows.
- **No background process when idle.** The web UI is only running when you run
  `make serve`. Scheduled scans are launchd's job, and they run the CLI directly.
- **HTTP cache** (in `watch_app/http.py`) keeps fetches cheap — a single 6-hour
  TTL across all sources, which makes a daily 7am scan nearly free after the
  first one. Second and later scans within 6 hours serve entirely from cache
  and typically complete in under a second. HTTP requests use per-phase
  timeouts (`connect=10s, read=30s, write=10s, pool=5s`) so a single hung
  source can't stall the whole scan — a real issue we hit when GPHG's server
  hung for 17 minutes during one scheduled run.
- **Why structured sources instead of pure extraction everywhere**: GPHG gives
  us brand-per-nomination directly. Parsing it through the general extractor
  would be strictly worse — we'd lose the prestige attribution and miss brand
  variants the extractor doesn't recognize.

## Development

```bash
make test         # Full suite; expects 279 passing
pytest -v tests/test_ranker.py  # Run one file
pytest -v -k hidden_gem         # Run tests matching a name
```

Key regression tests worth knowing about:

- `tests/test_seed_audit.py::test_all_known_conglomerates_are_in_seed_as_non_independent` —
  if you add a conglomerate name anywhere, this test fails until you seed it
  correctly.
- `tests/test_ranker.py::test_hidden_gem_zero_mention_always_beats_one_mention` —
  asserts the semantic invariant of the hidden_gem preset. Guards against
  regressions in the weight balance.
- `tests/test_methodology.py::test_worked_example_final_score_matches_ranker` —
  asserts the `/methodology` page's math equals the ranker's math to within
  1e-9. If the ranker is ever refactored in a way that diverges, the page
  would silently lie without this test.
- `tests/test_web_routes.py` — integration tests that spin up FastAPI and hit
  real routes. Guards against template-rendering bugs.

Adding a new source: inherit from `RSSSource` if it's an RSS feed (most media
sites), subclass `Source` directly for structured sites. Add to the
`default_sources()` list in `watch_app/pipeline/orchestrator.py`. The
extractor's vocabulary is built automatically from the Makers table.

Adding a new scoring preset: edit `watch_app/scoring/presets.py`. Any new
preset is immediately available in the CLI and web UI.

## License

TBD.
