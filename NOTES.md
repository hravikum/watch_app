# NOTES — future work and thinking

A running log of ideas and decisions. Things too small to be issues, too
important to lose in chat. Update freely; there's no ceremony here.

## When returning after a break

Before building anything new, spend 30 minutes just looking:

1. Open the dashboard (`make serve` → http://localhost:8000)
2. Check the top 20 under both presets. Does the ordering match your
   intuition? Are there indies you'd personally expect to see that aren't
   there? Absences tell you about source gaps.
3. Run `python -m watch_app.cli history` to confirm scheduled scans ran.
   Any failed status? Any missing days?
4. Check `sqlite3 data/watch_app.db "SELECT COUNT(*) FROM mentions;"` —
   is it growing? Roughly 5–25 new mentions per day is the expected rate.
5. Sanity-check that velocity_90d and first_seen_recency are now doing
   real work. Open a detail page for a maker who's ranked high on
   rising_star — are those two signals contributing, or is it still
   mostly prestige_weighted_mentions carrying them?

Only after that review should you pick what to build next.

## Near-term candidates (roughly prioritized)

### Reddit source — top pick for actual discovery value
r/watchmaking and r/horology surface indie makers earlier than trade
media. Literally before editors know. This is the highest-signal addition
for the actual discovery goal.

- Medium effort, probably 1–2 chunks
- Needs: Reddit API credentials, OAuth flow, careful thinking about
  prestige weight (forum buzz is noisier than SJX — maybe 0.4–0.5?)
- Titles are often lousy or jokey; excerpts matter more than for RSS
- Watch out for duplicate mentions (same post on multiple subreddits)

### Source auditing view on the methodology page
Extend /methodology to show per-source stats: mention count contributed,
prestige weight, which makers it surfaced first. Helps answer "is
Fratello actually pulling weight or mostly noise?" — lets you tune
prestige weights based on evidence instead of guesses.

- Low effort, ~half a chunk
- Purely additive — reads existing data, no new sources
- Good candidate when you want to *understand* rather than expand

### Rank-change tracking
Persist snapshots of the top-N per preset and render "Risers this week"
and "New this week" views. This is what turns the app from snapshot
viewer into trend tracker.

- DON'T BUILD until you have 2+ weeks of scan data — otherwise the
  "change" is just noise from initial data accumulation
- Medium effort once data exists, ~1 chunk
- Schema addition: a RankSnapshot table or just a materialized view
  over the existing Score.computed_at timestamps

### YouTube RSS
Every YouTube channel has a feed:
`https://www.youtube.com/feeds/videos.xml?channel_id=<ID>`
The indie watch YouTube scene (Bark & Jack, Teddy Baldassarre's indie
videos, Watchfinder's indie-focused content) publishes regularly and
often surfaces lesser-known makers.

- Tractable — no API keys, same RSS infrastructure we already have
- Low-medium effort, ~1 chunk
- Main work: curating channel list, figuring out prestige weights per
  channel (Bark & Jack 0.9? A random review channel 0.4?)

### Models per brand (Tier 1 built in 2J — future tiers TBD)
Built: GPHG-nominated watches surface on each maker's detail page with full
specs (case material/diameter, movement type/caliber, frequency, power
reserve, water resistance, complications, price, limited edition count,
image). Sourced from `gphg.org/en/watches/<slug>` detail pages, parsed by
`watch_app/sources/awards/gphg_detail.py`.

Scoped intentionally to GPHG nominees so every WatchModel row has a
verified source URL. This means:
- F.P. Journe, Voutilainen, De Bethune, Simon Brette etc. all get good
  coverage (they're GPHG-nominated regularly)
- Brands that never enter GPHG (or haven't yet) show no models — that's
  a known limitation, not a bug

**Possible future tiers if Tier 1 isn't enough:**
- LLM-extract model references from RSS articles (broader coverage,
  noisier data — would muddy the clean GPHG-only data, so worth doing
  only if Tier 1 demonstrably leaves gaps for makers we care about)
- Per-brand website scrapers (high maintenance, only justifiable for
  the most-tracked 10–20 makers)

Don't build either of those reactively. Use Tier 1 for a few weeks first
and see whether the gaps actually bother you.

## Sitting in the backlog (worth doing eventually)

### Fuzzy matching in the resolver
"Bovet 1822" vs "Bovet" — right now these are distinct until you
manually add an alias. Same for typos and spelling variants. Not broken,
just annoying. Could use rapidfuzz or similar for a soft-match pass
before falling back to auto-create.

### "Personal interest" state per maker
Mark makers as "already know" / "boring" / "keep eye on" so your top-20
reflects your taste, not just the data. Requires user state — local
config file or simple auth. Nice but not urgent.

### Automated seed entry
Paste a Wikipedia-style paragraph about a maker, have an LLM extract
canonical name, country, founded year, aliases, is_independent, and
append to the YAML. Quality-of-life for adding new seeds fast.
Non-trivial LLM integration.

### Cleanup of the XRBY / "Xrby La Montre D'Art" duplicate
Hotfix 2A left an orphaned DB row from before the title-casing fix
shipped. SQL to merge is in CHUNK_2A_HOTFIX_NOTES.md. Harmless but
untidy.

## Explicitly NOT doing

### Instagram ingestion
Instagram's API has been hostile to developers for years. Scraping is
fragile, ToS-violating, and brittle. Not worth it for a local tool.

### Charts on the dashboard
Still premature. Revisit when there are 3+ weeks of scan data and
rank-change tracking is in place.

### Search, filters, pagination
Top-20 fits on screen. Don't add these until the dataset makes
browsing painful — which it isn't.

### Public deployment
Haven't decided if this is a personal tool or something to share.
Deploying locks in "something to share" with real operational baggage
(hosting, rate limits, abuse, cost). Keep local until the identity is
clear.

### Default-on LLM extraction
The rule-based extractor works. LLM is available via `--use-llm` when
needed. Making it default costs money for marginal value at current
scale.

## Open questions to sit with

**What is this tool actually for?** Two identities pull in different
directions:
1. Personal discovery tool — rewards browsing polish, trends, maybe
   a notifications feature
2. Defensible public ranking — rewards more/better sources, source
   quality auditing, methodology transparency

Both are valid, they lead to different priorities. The choice doesn't
have to be made now, but watch for it when picking future features.

**Is the two-preset split still the right framing?** rising_star and
hidden_gem might converge or diverge interestingly over time with more
data. If the same names keep topping both, maybe there's only one
meaningful preset. If they stay distinct, the split is doing work.

**Are the prestige weights calibrated right?** SJX at 1.0, Monochrome
at 0.9, Hodinkee at 0.7 — these are guesses from Phase 1. After a few
weeks of data, a source-auditing view could validate or challenge them.

## Chunk delivery log

For quick reference on what shipped when:
- 0   Phase 0 scaffolding (dummy source, initial schema)
- 1A  HTTP client + seed YAML + rule-based extractor
- 1B+ GPHG scraper + expanded seed (+2 hotfixes)
- 1C  5 RSS sources: SJX, Monochrome, Fratello, Hodinkee, Quill & Pad (+2 hotfixes)
- 1D  7-signal scoring system with z-score normalization, two presets (+2 hotfixes)
- 2A  ScanRun table + history CLI + launchd schedule command (+1 hotfix)
- 2B  UI polish + maker detail pages
- 2C  README rewrite
- 2D  /methodology page with live worked examples
- 2E  README refresh post-methodology
- 2F  NOTES.md
- 2G  Hotfix: HTTP per-phase timeouts + GPHG awards-cap on prestige_weighted_mentions
- 2H  Source contributions table on /methodology
- 2I  README refresh post-2G/2H
- 2J  WatchModel layer: GPHG-nominated watches per maker, with specs, on detail page
- 2J-hotfix  Rewrite GPHG detail parser to read from Next.js Flight payloads
            (the original `<dl>` parser was based on incorrect fixture HTML;
            real pages embed structured data in `self.__next_f.push` calls
            with $LNN-referenced labels)
- 2J-hotfix2 Rewrite parser again, this time with a recursive value extractor
            that handles all six observed `field-items` shapes (plain
            strings, dangerouslySetInnerHTML spans, nested arrays with
            <br>, mixed arrays with empty leading entries, $LNN array
            references, $NN string references). Validated against the
            actual Parmigiani Tonda PF page.
- 2K  Comparison page at /comparison: every maker, both preset scores
      side by side, sortable by either preset's rank, by mentions, or
      by divergence (|rank_a - rank_b|). Surfaces makers the two
      presets disagree most about.
- 2L  Rank-change tracking at /movers: climbers, fallers, new entrants
      under either preset, configurable lookback (3/7/14 days), filtered
      by |rank change| ≥ 3 to suppress shuffle noise.
- 2M  Populate maker websites: 117 of 125 seed entries now have website
      URLs. Seed loader updated to backfill existing Maker rows, so
      the next scan picks up the new URLs without a migration. Detail
      page already had the (previously dormant) rendering logic.
- 2N  YouTube channels added as a new source category. 16 channels
      loaded from watch_app/data/youtube_channels.yaml, weighted from
      0.3 to 0.9 across three content tiers (indie-focused / broad /
      mainstream-dealer). Custom `_extract_excerpt` reads
      `media:description` (YouTube's Atom dialect); channel list is
      data-driven so adding channels later is a one-line YAML edit.
- 2N-hotfix  YouTube feed endpoint flakiness mitigation. Retry on
            HTTP 404/500 with short backoff (1s, 3s). Connection
            errors and non-retryable status codes (e.g. 403) bail
            immediately rather than waste time. Browser-like User-Agent
            on YouTube fetches (other tools report this doesn't always
            help but it's free).
- 2N-hotfix2 Two YouTube channel IDs corrected:
            - Watchfinder & Co.: was UCu16y62LPCwTknfV5_7Zalg (failed all
              retries on every scan), now UCLaoR2K7Dsa_KUK-DVpdZZA (the
              main UK channel).
            - WatchAdvisor: was UCF0mZ8eOx3ie5yCErYRoT5A (failed all
              retries on every scan), now UCFh-oKEXamVSLQRzG2_23wQ
              (Alexander Linz's Swiss media operation).
            YAML-only change. No code change.
- 2N-hotfix3 WatchAdvisor commented out in youtube_channels.yaml.
            Channel ID verified correct, but YouTube's feed endpoint
            persistently returns 404 even with retries while other
            channels succeed normally. Rather than waste ~5s every
            scan retrying, disabled. 15 channels active; YAML edit
            to re-enable if YouTube starts serving the feed.

## Findings logged from the week-1 data review (2G)

**HTTP timeout fix.** Two scheduled scans took 17 minutes and 37 minutes
respectively. Single GPHG fetch hung; httpx's float timeout didn't actually
enforce per-phase ceilings. Fixed with `httpx.Timeout(connect=10, read=30,
write=10, pool=5)`. If a future scan ever exceeds ~1 minute, something
unrelated is wrong.

**Parmigiani-style accumulation in prestige_weighted_mentions.**
Parmigiani Fleurier had 14 GPHG nominee URLs (they enter many watches
per year) which inflated their prestige_weighted_mentions to 16.8 vs.
Simon Brette's 1.2 — driving Parmigiani to #1 rising_star regardless
of other signals. Fixed by capping award-source contributions at one
per source row (each GPHG edition counts once even if a brand had 4
nominees that year). Media sources still uncapped — 4 SJX articles is
genuinely 4× the press signal of 1.

**`first_seen_at` is correctly preserved.** Initial review query was
poorly designed (8-day filter on a 5-day-old project) and made it look
like timestamps were being clobbered. They're not. False alarm.

**hidden_gem is genuinely working as designed.** Top 10 is all
zero-mention indies, all real makers, all credibly obscure. No fix needed.

**Quiet Club moved from hidden_gem to rising_star mid-review.** A new
SJX article on April 27 mentioned them in the context of F.P. Journe's
Young Talent Competition. They went from 0 mentions (hidden_gem rank
#6) to 1 mention (rising_star rank #12) in one scan. This is exactly
the discovery transition the system should surface. Pretty cool.

## Potential future scoring refinements (not urgent)

- **Mention deduplication across editions.** A brand nominated by GPHG
  in 2023, 2024, 2025 currently counts as 3 source contributions.
  Defensible (each year is a separate jury making a separate decision)
  but worth thinking about if the inflation looks wrong over more data.
- **`discovered_at` vs `published_at` in velocity calculations.** The
  90-day window uses `discovered_at`, which means historical articles
  ingested on day 1 count as "recent." Less of an issue once data has
  been accumulating organically for a couple of months.
- **Per-source prestige tuning based on actual data.** After 4–6 weeks,
  could analyze which sources surface makers that *also* appear in GPHG
  and use that overlap as evidence for prestige weight. Right now
  weights are best guesses.
