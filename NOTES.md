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

## Near-term candidates

### Reddit source (shipped in 2R — see chunk log)
Three subreddits (r/watchmaking, r/horology, r/Watches) ship as RSS-based
sources using the public `top/.rss?t=week` endpoint. No auth required.

**Possible upgrade if RSS limits become a problem:**
- Reddit's free legacy API (requires application + permission, days-to-weeks
  wait) would let us fetch more posts per subreddit and access richer
  per-post data (upvote counts, comment threads, top comments).
- RSS gets ~25 top posts per week per subreddit, which is plenty for
  daily scans of high-signal communities. Only upgrade if data shows
  we're missing meaningful coverage.
- Don't apply for legacy API unless we've used RSS for 2-3 weeks and
  identified specific gaps RSS can't fill.

**Watch out for:**
- Same post linked across multiple subreddits → existing URL dedupe
  in the resolver should handle this
- Subreddit name capitalization (r/Watches vs r/watches) matters for
  Reddit's URL routing — verified working in 2R

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
  the most-tracked 10-20 makers)

Don't build either of those reactively. Use Tier 1 for a few weeks first
and see whether the gaps actually bother you.

### Per-source detail page
Drill into "show me everything Hodinkee surfaced this week" from the
methodology page. Source contributions table already has summary stats;
clicking a source row could open a list of its recent mentions.

- Low effort, ~half a chunk
- Useful when you want to evaluate source quality qualitatively, not
  just by aggregate stats

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

## The bigger arc: discovery → engagement

Captured 2026-05-11. The current tool surfaces makers. The longer arc
is to help users *figure out which makers they'd be interested in*
and eventually to act on that — contact, relationship, becoming part
of a maker's story. Reddit and the other near-term items above
should ship first; this arc is the next major direction after those.

### The reframe

"Build features for discovery" and "build features for the outcome
(engagement)" sound separate but aren't. The bridge between them is
the data that lets a user navigate by their own taste — not just
consume a global ranking. Without that, the tool answers "which
makers are rising globally?" but not "which makers would I be
interested in?" Two different users with very different tastes get
the same answer today. That's the gap to close.

### Three flavors of "interested in"

The word is doing too much work. Distinguishing helps:

1. **Aesthetic interest** — classical vs modern, vintage-inspired vs
   avant-garde, dressy vs tool, finishing-led vs complication-led
2. **Philosophy / story interest** — solo watchmaker vs small atelier
   vs heritage relaunch vs microbrand; who's behind the work
3. **Practical fit** — price band, output scale, whether you can
   actually buy/commission/access

Correlated but not the same. A user expressing "interested in" usually
means a weighted combination of all three.

### Three layers of work

**Layer 1: enrich Maker data with the dimensions people care about.**
Add structured fields capturing aesthetic style, brand philosophy /
type, price band, output scale, distribution model. Multi-tag rather
than single-value — aesthetic in particular is too contested to force
into one tag per maker. Approximate vocabulary:

- aesthetic_tags: classical, modern, avant-garde, vintage-inspired,
  tool, dressy, finishing-led, complication-led
- maker_type: solo-watchmaker, small-atelier, heritage-relaunch,
  microbrand, established-indie, conglomerate-independent
- price_band: entry ($500-2k), mid ($2k-10k), high ($10k-50k),
  haute ($50k+)
- output_scale: one-off, sub-50/yr, 50-500/yr, 500+/yr
- distribution: direct, dealer-only, gallery, allocation-waitlist

153 makers × ~5 fields × 1-3 tags each = ~1500 data points.
Real work. Probably some mix of LLM-assisted bulk classification
with verification, seed YAML extension, and "this changes rarely so
curate once."

**Layer 2: build the multi-filter explorer that uses the new
dimensions.** Pick the dimensions that matter to you, see makers
ranked within that slice. "Solo watchmakers with classical aesthetic
in the $10-50k band who are accessible" → Brivet-Naudot, Strehler,
Auffret Paris, etc. Genuinely different from the current dashboard —
treats users as having taste rather than serving everyone the same
ranking.

Layer 2 also enables the "similar to X" feature using structured
dimensions as a similarity metric. Strehler-like makers, Voutilainen-
like makers. Probably worth shipping inside Layer 2 since they share
the same data.

**Layer 3: capture user reactions to feed back into the model.**
Once Layer 2 exists, the next question is whether user preferences
actually matched the maker once they investigated. Thumbs up / down
or "interested" / "not interested" per maker. Over time, learn what
dimensions correlate with that user's actual reactions. This is
where the tool becomes *adaptive*. Real value but premature without
Layer 2 in use first.

### Sequencing

Layer 1 is the foundation. Without it, Layer 2 and 3 have nothing
to work with. Layer 1 itself splits into:

- **1a — Schema + display:** add fields to Maker, render on detail
  page, populate ~10-20 well-known makers manually via seed YAML.
  Ship. The point is to make the data visible and usable even before
  the explorer exists.
- **1b — Bulk population:** populate the remaining ~135 makers.
  LLM-assisted likely. Verify before commit. Multi-stage.
- **2 — Multi-filter explorer:** UI that lets users navigate by
  these dimensions. Includes similarity ("makers like X").

Probably 4-6 weeks of incremental work across multiple chunks. Real
scope but matches the pace we've been keeping.

### Open design questions for when we get here

- **Vocabulary stability.** Once tags are committed and users start
  filtering by them, renaming/restructuring becomes expensive. Worth
  more thinking upfront than the schema would normally get. Maybe
  ship 1a deliberately small (3-4 well-defined tags per dimension)
  and expand only when usage reveals gaps.
- **Subjective vs objective fields.** `price_band` is more objective
  than `aesthetic_tags`. Worth distinguishing in the UI — maybe
  aesthetic tags appear as "[claude] dressy, classical" or similar,
  signaling judgment vs fact. Or accept that all of it is at least
  partly judgment and don't over-annotate.
- **Public vs personal use.** If the explorer ships to public users
  someday, taste tags become contested in a way they aren't for
  personal use. The multi-tag approach (multiple tags per maker)
  hedges against this — a user looking for "classical" finds the
  makers tagged classical, even if those makers are also tagged
  modern. Better than forcing a single bucket.

### Practical groundwork worth doing during interim

Before any Layer 1 code:

- Pick 5-10 makers as a personal "target list" — ones you'd
  actually want to engage with. Use the tool's data as input, your
  own judgment as filter.
- For 1-2 of them, do the engagement research manually: sales
  model, contact path, output, philosophy. Write it down somewhere.
- Notice what data was hard to find or what categories don't fit
  the dimensions listed above. That feedback shapes Layer 1's schema
  before we commit to it.

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

**What is this tool actually for?** Initial framing was a binary:
1. Personal discovery tool — rewards browsing polish, trends
2. Defensible public ranking — rewards more/better sources, source
   quality auditing, methodology transparency

That binary was wrong. The actual goal (captured 2026-05-11) is a
third thing: a tool that helps users discover makers *they* would
be interested in, and eventually act on that (contact, engagement,
journey). Discovery and public ranking aren't competing identities;
they're sequential layers under a larger goal. See "The bigger arc"
section above.

The remaining question is whether this stays personal or goes
public. Both are still on the table. Public deployment introduces
operational, legal, and maker-rights complications worth considering
before committing.

**Is the two-preset split still the right framing?** rising_star and
hidden_gem might converge or diverge interestingly over time with more
data. If the same names keep topping both, maybe there's only one
meaningful preset. If they stay distinct, the split is doing work.

**Are the prestige weights calibrated right?** SJX at 1.0, Monochrome
at 0.9, Hodinkee at 0.7 — these are guesses from Phase 1. YouTube
channels added in 2N use guessed weights too (0.3-0.9 across three
tiers). After a few weeks of data, the source contributions table on
`/methodology` can validate or challenge these — channels with high
volume but low first-surfaced counts are noise candidates; the inverse
are quiet but high-signal sources worth raising.

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
      0.3 to 0.9 across three content tiers. Custom `_extract_excerpt`
      reads `media:description` (YouTube's Atom dialect); channel list is
      data-driven so adding channels later is a one-line YAML edit.
- 2N-hotfix  YouTube feed endpoint flakiness mitigation. Retry on
            HTTP 404/500 with short backoff (1s, 3s). Connection
            errors and non-retryable status codes (e.g. 403) bail
            immediately. Browser-like User-Agent on YouTube fetches.
- 2N-hotfix2 Two YouTube channel IDs corrected (Watchfinder & Co. and
            WatchAdvisor). YAML-only.
- 2N-hotfix3 WatchAdvisor commented out — channel ID verified correct
            but YouTube's feed endpoint persistently 404s while peer
            channels succeed. 15 active YouTube channels remain.
- 2O  Documentation refresh — README and NOTES updated to reflect
      shipped 2K-2N work, current source counts, current test counts,
      and feature inventory. Doc-only chunk.
- 2P  Discovery → engagement arc captured in NOTES.md as the next
      major direction after Reddit. Three flavors of "interested in"
      (aesthetic, philosophy, practical fit); three layers of work
      (enrich Maker data with multi-tag taste dimensions, build
      multi-filter explorer, capture user reactions). Doc-only.
- 2P-hotfix Corrected Auffret Paris website URL in seed YAML
            (auffretparis → auffret-paris). One-line fix.
- 2Q  LLM-driven maker enrichment via `python -m watch_app.cli enrich`.
      For makers with `website IS NULL`, asks Haiku 4.5 for the official
      URL, validates it isn't a blocked aggregator/dealer domain,
      verifies it actually resolves over HTTP, and (with --commit) writes
      to DB. Default is dry-run. LLM responses cached so re-running is
      free. Solves the auto-discovered makers gap: investigative leads
      now have working website links.
- 2R  Reddit subreddits added as RSS-based sources. Three subreddits
      (r/watchmaking, r/horology, r/Watches) using the top/.rss?t=week
      URL form to filter to community-upvoted posts. Hardcoded one
      class per subreddit (same pattern as SJX/Monochrome/etc.) rather
      than data-driven YAML — Reddit list is short and stable. Used
      Reddit's public RSS feeds, no auth required, no waiting for
      legacy API permission. Prestige weights 0.4-0.6, intentionally
      below all journalism sources.
- 2S  Discovery view (`/discoveries`) for auto-discovered makers — those
      the resolver created from article mentions that aren't in the seed
      YAML. Sorted by first_seen_at desc. Shows what we know (country,
      founded_year, website, independence), which fields came from LLM
      enrichment vs seed/manual, mention count, sources. Visibility layer
      over what the tool has already learned. Added enriched_at and
      enriched_fields columns on Maker to track LLM-enriched data;
      cli enrich now populates them. Definition A: a maker is auto-
      discovered if its canonical_name doesn't match seed canonical names
      or aliases. Schema migration 0005 adds the columns.
- 2U  Subject-extraction trigger. The rule-based extractor fails when
      an article is about an unknown indie but mentions established
      brands as stylistic references — e.g., the Hodinkee Mermont La
      Parfaite article got attributed to Breguet because "Breguet-style
      numerals" appeared in the body. Added watch_app/extraction/subject.py
      which detects when a journalism title prefix ("Introducing:",
      "Hands On:", "First Look:", "In Depth:", "Meet") matches AND all
      rule-based attributions are to "common reference" makers (Rolex,
      Patek, Omega, Breguet, etc. — 13 brands). When both conditions
      hold, calls the existing LLM entity extraction from 2N and
      REPLACES the common-reference attributions with the LLM's
      subject identification. Conservative trigger to bound LLM cost
      to ~5-15 calls per scan. Backfill script processes historical
      data once (~$0.25 one-time). Added Mermont to seed YAML.
- 2T  Multi-field enrichment + automatic on-scan integration.
      Extends 2Q's pattern from website-only to website + country +
      is_independent. Refactored extraction/enrich.py to a field-
      registry pattern (FIELDS list with per-field lookup_fn,
      verify_fn, auto_enrich flag) so adding founded_year later
      is one entry. Country verified against a known-watchmaking-
      countries list; is_independent uses strict YES/NO/NONE.
      auto_enrich_unenriched_makers() runs after scoring in the
      orchestrator, capped at 25 makers per scan to bound cost.
      `cli enrich` now lists per-field proposals in the dry-run table.
      All 2Q tests still pass via backwards-compat shim on
      EnrichmentProposal.
- 2V  Scan phase-timing instrumentation + YouTube 404 retry fix.
      Investigation started from "the LLM is slow in the daily run" but
      phase timing disproved it: a `_phase_timer` context manager in
      run_scan() now logs `phase=NAME duration=Xs` for seed/sources/
      gphg_models/scoring/enrichment, plus per-source timing inside the
      sources loop. Revealed the sources phase is ~99% of run time and
      the cost is upstream HTTP (YouTube RSS outage, GPHG slowness), not
      the LLM (which runs ~1s/call, ~5 calls/scan). Second change:
      dropped 404 from YouTube's RETRYABLE_STATUS_CODES (5xx only) since
      outage 404s never recover within a scan's retries — saves ~50s on
      bad-window days. Channel IDs for the 0-mention channels were
      verified correct (not changed). No DB changes, no new deps.
- 2V-doc  README + NOTES updated for 2V: corrected the HTTP read timeout
      reference (30s → actual 10s), updated the YouTube source description
      to 5xx-only retry, added a "debugging slow scans" note pointing at
      the phase= logs, revised the stale "scan over 1 minute = something
      wrong" guidance, and expanded the YouTube finding with the
      intermittent-outage understanding. Doc-only.
- 2W  Scheduled scan time moved from 7 AM to 9 PM. Changed the Hour in
      the StartCalendarInterval block of the launchd plist generated by
      `cli schedule` (7 → 21) and updated the two console.print strings
      that announce the time. The live installed plist at
      ~/Library/LaunchAgents/com.user.watchapp.scan.plist was edited
      directly and reloaded via launchctl unload/load — the code default
      had drifted from the live value (code said 7, live ran at 9), so
      both are now aligned at 21. Laptop-asleep catch-up still applies:
      a 9 PM slot fires on next wake if the Mac is asleep at the hour.
      No DB changes, no new deps.

## Findings logged from the week-1 data review (2G)

**HTTP timeout fix.** Two scheduled scans took 17 minutes and 37 minutes
respectively. Single GPHG fetch hung; httpx's float timeout didn't actually
enforce per-phase ceilings. Fixed with `httpx.Timeout(connect=10, read=10,
write=10, pool=5)`. Note (updated 2V): the old guidance here — "if a scan
exceeds ~1 minute something is wrong" — turned out to be too strong. Scans
can legitimately reach 2+ minutes during upstream HTTP trouble (the YouTube
RSS outage, GPHG slowness) without anything being wrong on our side. Use the
`phase=` timing logs (2V) to tell a genuine problem from an upstream bad
window before assuming a regression.

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

**YouTube feed endpoint is unreliable (2N).** YouTube's
`/feeds/videos.xml?channel_id=<ID>` endpoint intermittently returns
404 and 500 for valid, active channel IDs. Same channel works fine
in a browser at the same moment. Other RSS readers (miniflux,
RSS-Bridge, n8n) report the same pattern. Mitigated with retry +
backoff in 2N-hotfix. Diagnostic rule: a channel that fails *all*
retries across *multiple* scans while peer channels recover is
probably a wrong channel ID (correct fix in 2N-hotfix2). A channel
where the ID is verifiably correct but the endpoint still 404s
persistently is a YouTube-side problem worth disabling (2N-hotfix3
on WatchAdvisor).

Updated 2V: the failure is better understood as a broad *intermittent
outage* than a per-channel issue. Observed one scan where all 16
channels returned 404/500, then the very next scan (same IDs, no code
change) where all 16 returned 200 — and the bad window coincided with
GPHG read-timeouts and Reddit 403s, suggesting whole-window network
trouble rather than three independent upstream problems. Consequence
for the earlier diagnostic rule: a single bad scan proves nothing about
a channel ID now, because *every* channel can 404 in a bad window. Only
distrust a channel ID if it fails while *peers in the same scan*
succeed. We re-verified the Urban Gentry / Time Teller / Worn & Wound
IDs (all 0-mention at the time) against YouTube's handle resolution —
all three were correct and returned 15 entries each once the outage
lifted. Also in 2V: stopped retrying 404 (5xx only), since outage 404s
are sustained across a scan's retries and never recover within it —
retrying just burned ~50s/scan on bad-window days for no benefit.

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
