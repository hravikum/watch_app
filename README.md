# Watch App

A local tool for discovering up-and-coming independent watchmakers — the kind of
makers who might become nominees, finalists, or winners of prizes like the Louis
Vuitton Watch Prize for Independent Creatives.

The app ingests from watch media and award nominations, resolves mentions against
a curated seed of ~240 known makers (plus auto-creates new ones it sees), scores
each maker under two rankings, and surfaces results in a CLI and local web UI.

## What it does today

- **Ingests from ~26 sources daily**: 5 watch publications via RSS (SJX,
  Monochrome, Fratello, Hodinkee, Quill & Pad), 3 GPHG editions via a
  scraper of the official site (2023/2024/2025), 3 Reddit subreddits
  (r/watchmaking, r/horology, r/Watches), and 15 YouTube channels
  covering indie watchmakers across three prestige tiers.
- **Tracks ~240 curated seed makers** plus anyone new that surfaces from GPHG or
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
- **Carries curated taste dimensions per maker.** Each independent maker
  can be tagged across seven character dimensions — aesthetic (classical,
  modern, avant-garde, vintage-inspired, tool, dressy), what they're known
  for (time-only, chronograph, tourbillon, calendar, chiming, astronomical),
  maker type, price band, output scale, distribution, and movement origin.
  They render as a "Profile" panel on the detail page. These are descriptive,
  for navigating by taste — they do **not** feed the preset scores (only the
  `/for-you` re-ranking reads them, alongside your reactions). The controlled
  vocabulary lives in `watch_app/taste.py`; the seed loader validates every
  tag against it.
- **Drafts taste tags via LLM for review.** `python -m watch_app.cli
  draft-taste` walks untagged independent makers, asks Haiku 4.5 for the
  seven dimensions constrained to the vocabulary (abstaining when unsure),
  and — with `--commit` — writes drafts into the seed YAML for human review
  via `git diff`. Conglomerate brands are skipped. Curation aid, not a
  runtime path.
- **Compares makers across both presets** at `/comparison`. One sortable
  table with every maker's `rising_star` rank, `hidden_gem` rank, and the
  divergence between them. Sorting by divergence surfaces makers the two
  presets disagree most about — usually the most informative population to
  investigate.
- **Tracks rank changes over time** at `/movers`. Climbers, fallers, and
  new entrants over a configurable lookback window (3, 7, or 14 days)
  under either preset. The "rising star" preset's purpose is finally
  visible as actual movement, not a static snapshot.
- **Charts the longer arc** at `/trends`. Two SVG charts — mention volume
  per day, and rank trajectories for the current top 10 makers traced back
  through their stored score history — with hover tooltips, hover-to-
  highlight one maker through the tangle, and click-a-swatch to hide a line.
  Where `/movers` gives week-over-week numbers, this is the time-series
  picture.
- **Summarizes what changed** at `/digest` (and `cli digest`). A weekly
  "what changed" recap over a configurable window: new discoveries, rank
  movers, and — the personal part — activity on the makers you've marked
  watching or target (new mentions, rank moves, or a brand-new discovery),
  with quiet makers omitted so it reads as an alert. `cli digest --markdown`
  emits a portable markdown export you can pipe to a file or email. No new
  data — it composes the discovery and rank-change queries you already have.
- **Emails the digest** via `cli send-digest`. A summarized HTML body with the
  full digest attached as a styled HTML file, over stdlib SMTP. Dry-run by
  default (writes a preview, sends nothing); `--send` delivers. See
  [Email digest](#email-digest) for setup.
- **Links to maker websites** on every detail page where one exists. 222
  of 239 seed makers have a website URL — most of the rest (e.g. Philippe
  Dufour) genuinely don't have a public web presence.
- **Surfaces auto-discovered makers** at `/discoveries`. Any brand the
  resolver caught from an article that isn't in the curated seed YAML
  appears here, sorted by most recently found. The page shows what's
  known about each (country, founded year, website, independence,
  mentions, sources) and marks any field that was filled by LLM
  enrichment so you know what to verify.
- **Auto-enriches missing fields on scan.** After each scan completes,
  any newly-discovered Maker with `enriched_at IS NULL` gets enriched
  via Haiku 4.5 for three fields: website (HTTP-verified), country
  (known-list validated), and is_independent (strict YES/NO/NONE).
  Capped at 25 makers per scan. Manual `cli enrich --commit` still
  works for one-off backfills. Neither touches a maker you've already
  saved in `/review`, and the taste tagger below skips those too.
- **Auto-tags taste for discovered makers.** After the factual
  enrichment phase, any auto-discovered independent that hasn't been
  taste-attempted yet gets its seven taste / character dimensions
  drafted via Haiku 4.5, constrained to the controlled vocabulary and
  abstaining when unsure. The prompt is informed by the maker's recent
  press mentions (titles + excerpts), which lets it confidently tag niche
  brands it wouldn't know by name alone. Written tags are flagged the
  same "auto" way for review. Gated by `taste_enriched_at` (stamped once
  per maker, even on abstention) so nothing is re-queried; capped at 25
  per scan. `cli enrich --taste` backfills older discoveries; add
  `--retag` to re-attempt makers that previously abstained.
- **Review queue (admin).** `/review` is a queue of the discovered makers
  nobody has checked yet, with a running "N to go" count — ranked makers
  first (in rank order under your default preset), then the unranked tail
  newest-first. Each card leads with the evidence the tool found the maker
  on (rank, mention count, sources, the latest article titles as links, the
  website as a link) and a summary of its current values: anything LLM
  enrichment filled that nobody has checked is marked "auto" in amber, and
  blank fields are listed. If it's right, Save; if not, fix it under
  "Edit fields" first (taste dimensions and factual fields are all
  editable). Saving overwrites the drafts, clears the "auto" flags, and
  signs the maker off: it leaves the queue (`/review?show=reviewed` lists
  reviewed makers for corrections) and enrichment never touches it again.
  Save, Suppress, Delete and Promote all happen in place — the card
  collapses to a ✓ row and the count ticks down without a page reload — and
  anything refused (a name collision, say) shows inside the card without
  losing your edits. Gated behind
  `WATCH_APP_ADMIN` (off by default, so it's never exposed on a public
  deployment). Seed makers are excluded — their taste stays curated in
  the YAML. Each card also carries per-maker Suppress / Delete controls
  (see "Suppress or delete a maker" below). If a scan grabbed a maker's
  name wrong, fix it in the card's Name field: mentions and scores stay
  with the maker, and by default the old name is kept as an alias so later
  scans resolve it to the corrected maker rather than re-creating the bad
  row (untick the box when the old name was junk rather than a real
  spelling). A name that already belongs to another maker is refused —
  that's a duplicate, so merge instead. `cli rename` does the same from
  the terminal.
- **Explore by taste** at `/explore`. Faceted filtering over every maker
  that carries taste tags (seed and discovered together): OR within a
  dimension, AND across, with a static count beside each option so you
  can see what's available. Matches are ranked by any scoring preset, so
  you can ask for "the rising stars among classical solo-watchmakers."
  Untagged makers are absent — nothing to filter them on. A **Window**
  control narrows the universe by recency — All-time / 3 / 7 / 14 / 30
  days, on either basis: Mentioned (most recent press) or Discovered
  (first seen by the tool). A **Your list** control narrows to a personal
  relationship state (see below).
- **"Similar makers"** on each maker's detail page. Surfaces the closest
  makers by taste — weighted overlap across the seven dimensions (Jaccard
  on the multi-value ones, exact match on the rest), weighted toward
  aesthetic, type, complications, and movement origin, and normalized
  over the dimensions both makers actually have tagged. Each match shows
  the shared characteristics that drove it.
- **Promote a discovery to seed** — once a discovered maker proves worth
  keeping, `cli promote "<name>"` (or the per-maker button in `/review`)
  serializes its curated fields and taste into a new `known_makers.yaml`
  entry, which you review with `git diff` and commit. It then reconciles
  onto the existing DB row on the next scan and joins the curated set.
  Dry-run by default; blocks exact name collisions and asks you to
  confirm fuzzy near-matches.
- **Mark your own relationship to a maker.** Each detail page has a "Your
  status" form: set a relationship state — target / watching / known /
  pass — plus free-text notes (engagement research, contact path, why
  you're watching). State shows as a small badge on the dashboard,
  explorer, and discoveries rows, and you can filter the dashboard
  ("Your list") and the explorer ("Your list" in the sidebar) by it.
  This is single-user/local and orthogonal to suppression: it's your
  posture toward a *real* maker, and it never changes visibility on its
  own. Note the dashboard filter is a ranking view, so a maker you've
  marked but that isn't scored yet won't appear there (the detail page
  and explorer still show it). The same "Your status" form also takes a
  **taste reaction** — like / dislike on the maker's *work* — kept separate
  from the relationship state (posture toward the maker) and from suppression
  (whether it's in the tool at all). It records when you reacted and is the
  signal the `/for-you` view re-ranks on.
- **Triage your reactions in one pass** at `/triage`. A queue of every
  maker you haven't reacted to yet (suppressed makers excluded), scored
  ones first in rank order under your default preset, then the unranked
  long tail newest-first. Each card shows just enough to judge — country,
  type, aesthetic tags, rank/score/mentions — with **Like** / **Dislike**
  buttons; reacting drops the maker from the queue and ticks the remaining
  count down without a full reload. Skipping is just leaving a card alone —
  it returns next time. To change or clear a reaction, use the maker's
  detail page. This is the fast way to build up the like/dislike signal
  `/for-you` reads.
- **A ranking tuned to your taste** at `/for-you` (first link in the nav).
  It takes either preset's ranking and re-orders it by your reactions: each
  maker's taste tags are compared with the makers you've liked and disliked
  (the same similarity that powers "Similar makers"), and that affinity is
  z-scored and blended into the preset score at a fixed weight
  (`AFFINITY_WEIGHT`, 0.6, in `scoring/affinity.py`). Makers close to your
  likes climb, makers close to your dislikes sink, and untagged makers hold
  their place. The table shows the affinity, the closest liked maker as the
  "why", and each maker's original rank. Computed on every page load, so it
  shifts the moment you react; with no reactions yet it points you to
  `/triage`. Eligibility (rising_star's independent gate, hidden_gem's
  mention cap) carries over from the base preset.
- **Suppress or delete a maker.** When a discovered name isn't worth
  tracking — a defunct historical brand, something out of scope —
  `cli suppress "<maker>"` hides it from `/discoveries`, the rankings,
  `/comparison`, and `/review` without deleting it: the row and mentions
  survive, so the resolver re-matches (not re-creates) the name on later
  scans and a one-off article can't resurrect it. Fully reversible with
  `cli unsuppress`. Suppressed makers are also skipped by on-scan
  enrichment and taste-tagging, so they stop costing LLM calls.
  `cli delete` is the hard path — removes the maker and everything that
  references it, and tombstones the name (plus its aliases) in the
  `blocked_makers` table so the resolver won't re-create it when a live
  article still mentions it. The block uses the same normalize + fuzzy
  matching the resolver does, so variant spellings can't resurrect it
  either; `cli blocked` lists the tombstones and `cli unblock` reverses
  one. (Adding a blocked name to the seed YAML deliberately overrides the
  tombstone — an explicit curation decision wins.) Both
  are dry-run by default. With `WATCH_APP_ADMIN` on, both are also on each
  maker's detail page: a reversible Suppress / Unsuppress toggle (with an
  optional reason), and a Delete that opens a confirm panel showing the exact
  dry-run impact — how many mentions, watch models, signals, and scores would
  go — before it commits. A seed maker shows the blocker there instead of a
  confirm, same as the CLI. Both are also on the `/review` grid — one Suppress
  button and one Delete&hellip; link per card — so you can curate a discovered
  maker without opening its page: suppressing drops it off the grid, and
  Delete&hellip; opens the same dry-run confirm panel inline in that maker's
  card before it commits. Merge is on the detail page too, for the related
  case where a maker isn't actually new — a duplicate or name variant: it
  offers near-duplicate suggestions plus a name-search typeahead to pick the
  target, then a confirm panel showing what moves (mentions, watch models,
  aliases) before folding this row into it.
- **Catches new makers in debut articles.** When an article title
  starts with a journalism convention marker like "Introducing:",
  "Hands On:", or "First Look:" AND the rule-based extractor only
  found "common reference" brands (Rolex, Patek, Breguet, etc.),
  triggers an LLM pass to identify the article's actual subject.
  Prevents articles like "Introducing: The Mermont La Parfaite..."
  from being mis-attributed to Breguet because of "Breguet-style
  numerals" in the body.

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

The daily scan is rule-based (free): it only matches makers already in the
seed. To also surface *new* makers, schedule a weekly LLM discovery scan
alongside it:

```bash
python -m watch_app.cli schedule-discovery     # weekly `scan --use-llm`, Mon 06:00 by default
```

This runs `scan --use-llm` once a week (tagged `trigger=discovery` in the
history), just before the Monday scan/digest so new makers flow through. It
spends Haiku credits per run and will surface noisy makers to triage — that's
the intended discover-then-curate loop. It complements, and does not replace,
the daily scan.

Check run history any time:
```bash
python -m watch_app.cli history
```

## Email digest

`send-digest` emails you the weekly "what changed" recap: a summarized,
nicely-formatted HTML body (leads with your watching/target makers, then a
capped New discoveries + Movers summary) with the **full** digest attached as
a self-contained styled HTML file you can open in any browser.

It's **dry-run by default** — it writes the body and attachment to disk and
sends nothing, so you can preview before (and without) wiring up mail:

```bash
python -m watch_app.cli send-digest --out /tmp        # preview only
open /tmp/digest-email-*.html                          # view the body
```

Other options: `--days N` (window, default 7), `--preset rising_star|hidden_gem`,
`--to someone@example.com` (recipient override), `--cap N` (max rows per
section in the body, default 5), and `--send` to actually deliver.

### Sending for real (SMTP)

Sending uses stdlib SMTP — no extra dependencies. Add these to your `.env`
(only the first four are required; the rest have working defaults):

```bash
WATCH_APP_SMTP_HOST=smtp.gmail.com
WATCH_APP_SMTP_USER=you@gmail.com         # the account you send from / log in as
WATCH_APP_SMTP_PASSWORD=abcdefghijklmnop  # a Gmail App Password, NOT your login password (strip spaces)
WATCH_APP_DIGEST_TO=you@gmail.com         # where the digest lands

# Optional (defaults shown):
# WATCH_APP_SMTP_PORT=465                 # 465 = SSL; use 587 with the next line for STARTTLS
# WATCH_APP_SMTP_SSL=true                 # set false for STARTTLS on 587
# WATCH_APP_DIGEST_FROM=you@gmail.com     # defaults to WATCH_APP_SMTP_USER
```

Then:
```bash
python -m watch_app.cli send-digest --send
```

If the credentials are missing, the command names exactly which env vars to set
and sends nothing.

**Gmail app password:** Google requires an App Password (a 16-character code)
rather than your normal password for SMTP, and it needs 2-Step Verification
enabled first. Turn on 2-Step Verification under
`myaccount.google.com/security`, then create the password at
`myaccount.google.com/apppasswords` (name it something like `watch-app`), and
paste it — spaces removed — into `WATCH_APP_SMTP_PASSWORD`. App passwords are
unavailable for Google Workspace (work/school) accounts unless an admin allows
them; use a personal `@gmail.com` account in that case. The credentials live
only in your `.env` and are never committed.

### Scheduling it weekly

Once a manual `send-digest --send` works, schedule it with `schedule-digest`,
which mirrors the daily-scan `schedule` command but on a weekly cadence:

```bash
python -m watch_app.cli schedule-digest          # prints a launchd plist + steps
```

It defaults to **Monday 08:00** (just after the 7am scan, so the data is fresh
and the 7-day window lines up); override with `--day mon|tue|…`, `--hour`
(0–23), `--minute`. Save the printed plist to
`~/Library/LaunchAgents/com.user.watchapp.digest.plist` and load it:

```bash
mkdir -p ~/Library/LaunchAgents
COLUMNS=400 python -m watch_app.cli schedule-digest > /tmp/_wa.txt
sed -n '/BEGIN PLIST/,/END PLIST/p' /tmp/_wa.txt | sed '1d;$d' \
  > ~/Library/LaunchAgents/com.user.watchapp.digest.plist
launchctl load ~/Library/LaunchAgents/com.user.watchapp.digest.plist
```

It runs under its own label (`com.user.watchapp.digest`), independent of the
scan agent, and logs to `data/digest.log`. **To change the time later:**
regenerate the plist with new flags (or hand-edit the `Weekday`/`Hour`/`Minute`
integers), then `launchctl unload` and `launchctl load` it again — the reload
is what applies the change.

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
python -m watch_app.cli schedule                # Print launchd setup (daily rule-based scan)
python -m watch_app.cli schedule-discovery      # Print launchd setup (weekly `scan --use-llm` discovery)

python -m watch_app.cli digest                  # "What changed" recap (last 7 days, default preset)
python -m watch_app.cli digest --days 14 --preset hidden_gem
python -m watch_app.cli digest --markdown > this-week.md   # Portable markdown export
python -m watch_app.cli send-digest --out /tmp  # Email the digest — dry run (writes preview, sends nothing)
python -m watch_app.cli send-digest --send      # Email it for real (needs SMTP env vars; see Email digest)
python -m watch_app.cli schedule-digest         # Print launchd setup to email the digest weekly

python -m watch_app.cli source-quality          # Per-source signal-quality readout (calibrating prestige weights)
python -m watch_app.cli source-quality --days 14   # Restrict to the last 14 days (default: all-time)

python -m watch_app.cli enrich                  # Dry-run: propose website/country/independence for unenriched makers
python -m watch_app.cli enrich --commit         # Write proposals to DB

python -m watch_app.cli draft-taste             # Dry-run: propose taste tags for untagged independents
python -m watch_app.cli draft-taste --commit    # Write drafts into the seed YAML (review the git diff)
python -m watch_app.cli draft-taste --only "Grönefeld,Czapek"   # Draft specific makers
python -m watch_app.cli draft-taste --limit 10  # Cap the batch (useful for a pilot)

python -m watch_app.cli promote "<maker>"       # Dry-run: serialize a discovered maker as a seed YAML entry
python -m watch_app.cli promote "<maker>" --commit   # Append it to known_makers.yaml (review the git diff)

python -m watch_app.cli dupes                   # List near-duplicate makers (possible split rows) for review
python -m watch_app.cli merge <from> <into>     # Dry-run: fold one maker into another (mentions, aliases)
python -m watch_app.cli merge <from> <into> --commit
python -m watch_app.cli unalias "<maker>" "<alias>"  # Remove a bad alias (e.g. a wrong fuzzy merge); --commit to apply
python -m watch_app.cli rename "<maker>" "<new name>"  # Dry-run: fix a wrongly-grabbed name (old name kept as alias; --drop-old-name to skip); --commit to apply

python -m watch_app.cli suppress "<maker>" --reason "..."   # Dry-run: hide from discoveries/rankings (reversible); --commit to apply
python -m watch_app.cli unsuppress "<maker>"    # Restore a suppressed maker; --commit to apply
python -m watch_app.cli suppressed              # List currently-suppressed makers
python -m watch_app.cli delete "<maker>"        # Dry-run: hard-delete a discovered maker + all its rows, and block the name from re-discovery; --commit to apply
python -m watch_app.cli unblock "<name>"        # Remove a delete tombstone so the name can be re-discovered; --commit to apply
python -m watch_app.cli blocked                 # List names blocked from re-discovery by past deletes

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

## Maker taste dimensions

Separate from scoring, each independent maker can carry curated **taste
dimensions** — the data that lets you navigate by your own taste rather than
only a global ranking. They're shown as a "Profile" panel on the maker detail
page, and they drive the `/explore` filters, the "Similar makers" panel, and
the `/for-you` re-ranking.

Seven dimensions, two multi-tag and five single-value, all optional:

| Dimension | Kind | Vocabulary |
|---|---|---|
| `aesthetic_tags` | multi | classical, modern, avant-garde, vintage-inspired, tool, dressy |
| `complication_focus` | multi | time-only, chronograph, tourbillon, calendar, chiming, astronomical |
| `maker_type` | single | solo-watchmaker, small-atelier, heritage-relaunch, microbrand, established-indie |
| `price_band` | single | entry (<$2k), mid ($2k–10k), high ($10k–50k), haute ($50k+) |
| `output_scale` | single | one-off, sub-50, 50-500, 500-plus (watches/yr) |
| `distribution` | single | direct, dealer-only, gallery, allocation-waitlist |
| `movement_origin` | single | in-house, modified-base, stock-base |

The vocabulary is intentionally small and lives in `watch_app/taste.py`, which
is the single source of truth. `validate_taste_fields()` rejects anything out
of vocabulary; the seed loader calls it per entry, and a test
(`test_committed_seed_yaml_is_all_in_vocabulary`) runs it across the whole
committed YAML so a typo fails the suite rather than silently fragmenting the
tags. These fields are descriptive judgment — they never affect the preset
scores.

Tagging was done in two passes: a hand-tagged core of well-known makers, then
an LLM-assisted bulk pass via `draft-taste` (Haiku, constrained to the
vocabulary, abstaining when unsure), with every draft reviewed in the YAML
before commit.

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
  surfaces feeds that have gone silent. Each source name links to a
  **per-source detail page** (`/sources/{id}`) showing its metadata, the
  makers it surfaced first, its most-covered makers, and recent mentions.
- **Worked Examples** — for each preset, the currently top-ranked maker is
  walked through end-to-end: raw signal values, z-normalized values, preset
  weight, and per-signal contribution summing to their final composite score

The page updates automatically when preset weights change in code. A
regression test (`test_worked_example_final_score_matches_ranker`) asserts
that the math shown on the page matches the math the ranker actually used —
to within 1e-9 — so the page cannot silently lie if the ranker gets refactored.

The same page is linked as "How is this scored? →" next to every maker's
signal breakdown on their detail page, so you can always find your way
back to the explanation. That detail-page breakdown is itself a full worked
example *for that maker* under each preset (raw → z-score → weight →
contribution → composite, with their rank), not just the raw signal values —
a per-maker regression test pins each shown total to the ranker's stored score
within 1e-9, same as the methodology page.

## How sources work

Each source is a Python class that yields `RawMention` objects. Sources fall into
three shapes:

- **RSS-based media sources** (`sources/media/*.py`) — inherit from `RSSSource`,
  specify a feed URL and prestige weight. Title and article text go through the
  rule-based extractor to find maker mentions against the maker vocabulary
  (every name and alias in the Makers table).
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
  retries on transient 5xx errors from YouTube's known-flaky feed endpoint
  (intermittent on valid channel IDs) and a custom `_extract_excerpt` that
  reads `media:description` from YouTube's Atom dialect. 404s are not
  retried — during the 2025-2026 outage they are sustained across retries,
  so retrying only wastes backoff time. Adding a channel is a one-line YAML
  edit, no code.

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
│                         # (and won't re-create a name blocked by a past delete);
│                         # taste_draft.py drafts taste tags via LLM into the seed YAML (Layer 1b);
│                         # enrich.py fills factual fields + auto-tags taste on scan;
│                         # promote.py graduates a discovery to seed; maker_admin.py has
│                         # merge/unalias/rename/dupes + suppress/unsuppress/delete/unblock + relationship-state helper
├── taste.py              # Controlled vocabulary for the 7 taste dimensions + validation (single source of truth)
├── llm/                  # Anthropic client + content-hash response cache
├── scoring/              # signals.py computes the 7 signals; ranker.py composes them under presets;
│                         # methodology.py builds worked examples and source-contribution stats;
│                         # queries.py has fetch_top_makers, fetch_all_makers_comparison,
│                         # fetch_rank_changes, and the /trends queries used by the web UI;
│                         # affinity.py re-ranks a preset by your taste reactions (/for-you);
│                         # calibration.py is the read-only source-quality diagnostic;
│                         # discovery.py finds auto-discovered makers (not in seed YAML);
│                         # digest.py composes the weekly "what changed" digest + markdown export
├── storage/              # SQLAlchemy models (Maker, Mention, Signal, Score, ScanRun, Source, WatchModel,
│                         # BlockedMaker — delete tombstones, migration 0011).
│                         # Maker also carries the 7 taste-dimension columns (migration 0006),
│                         # suppression columns (0008), relationship_state + notes (0009),
│                         # taste_reaction + taste_reaction_at (0010), and reviewed_at (0012)
├── pipeline/             # orchestrator.py wires the layers: fetch → extract → resolve → store →
│                         # fetch GPHG model details → score
├── notify/               # email.py renders the digest email (summary HTML + full HTML attachment)
│                         # and sends it over stdlib SMTP (dry-run by default)
├── api/                  # FastAPI routers; makers.py has dashboard, detail (+ state form and
│                         # admin suppress/delete/merge), methodology, comparison, movers,
│                         # trends, for-you, triage, digest, discoveries, explore,
│                         # review (admin), and per-source routes
├── web/templates/        # Jinja2 + HTMX (dashboard.html, maker_detail.html, methodology.html,
│                         # comparison.html, movers.html, trends.html, for_you.html,
│                         # triage.html, digest.html, discoveries.html, explore.html,
│                         # review.html, source_detail.html, _makers_table.html,
│                         # _explore_results.html, _merge_results.html, _triage_reacted.html,
│                         # _review_done.html, _review_message.html, _review_delete_panel.html,
│                         # _macros.html)
├── web/static/           # trends.js — hover/highlight/toggle for the /trends charts (vanilla JS)
├── data/
│   ├── known_makers.yaml      # The ~240-maker curated seed, most independents now carrying taste tags
│   └── youtube_channels.yaml  # 15 YouTube channels with prestige weights and tier annotations
└── cli.py                # Typer CLI entry point

tests/                    # 691 tests; run with `make test` (~30s, no network)
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
  timeouts (`connect=10s, read=10s, write=10s, pool=5s`) so a single hung
  source can't stall the whole scan — a real issue we hit when GPHG's server
  hung for 17 minutes during one scheduled run.
- **Why structured sources instead of pure extraction everywhere**: GPHG gives
  us brand-per-nomination directly. Parsing it through the general extractor
  would be strictly worse — we'd lose the prestige attribution and miss brand
  variants the extractor doesn't recognize.

## Development

```bash
make test         # Full suite (~30s); expects 691 passing
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

Debugging slow scans: `run_scan()` emits one `phase=NAME duration=Xs` log
line per phase (seed, sources, gphg_models, scoring, enrichment), plus a
`phase=source name=X duration=Ys mentions=N` line per source inside the
sources phase. Grep `phase=` in the scan log to localize where time goes.
In practice the sources phase dominates, and almost all of its variance is
upstream HTTP behavior (the intermittent YouTube RSS outage, occasional
GPHG slowness) rather than anything in our code — a healthy-network scan
runs the sources phase in ~40s, a bad-window scan can be 3-4x that.

## License

TBD.
