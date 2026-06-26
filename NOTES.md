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

## Roadmap

Forward-looking only — open items, tiered by leverage *right now*. Completed
work lives in the chunk delivery log below; this section is kept to what's *not
yet built*. Tiered for the personal-tool posture (public deployment stays
parked — see "Explicitly NOT doing"); going public re-tiers everything
(personal-state features become multi-user/auth, source breadth + methodology
defensibility rise).

Status (2026-06-15): the discovery→engagement arc is nearly complete — Layers
1a/1b (taste schema + bulk tagging, 3A/3B), Layer 2 (the /explore filter +
similarity, 3O/3P), and Layer 3a (reaction capture 3V + triage 3X) all shipped.
Source-weight calibration is done end to end (diagnostic 3Y/3Z, tuning 4A), and
a weekly LLM discovery scan is now schedulable (4B). The maker-maintenance UI
(suppress/delete 4D, merge 4E) and the trends page (mention volume + rank
trajectories, 4F) have since shipped too. What's left is below.

### Tier 1 — strongest next bets
- **Layer 3b — adaptive ranking.** The arc's headline and the one piece left to
  finish it. Re-rank by each user's taste reactions, building on the captured
  like/dislike signal (3V) and the existing `_similar_makers` engine. Gated on
  reaction volume — still <20, but the weekly discovery scan (4B) + triage will
  fill that clock faster now. Build once there's signal to adapt to.

### Tier 2 — high value, sequence after a bit more data or groundwork
- **Engagement data per maker** (access / sales model / contact path / how to
  commission or buy). The "act on it" end of the arc. Do the manual research on
  a few makers FIRST (in the relationship-notes field, 3R) to learn which fields
  matter before committing schema, then schematize.
- **Daily-LLM cheap-discovery upgrade.** A skip-already-ingested-URL guard
  before the LLM extract call, so a *daily* `scan --use-llm` would be cheap AND
  complete (pay only for genuinely new articles). Only worth it if the weekly
  discovery cadence (4B) proves too coarse — otherwise weekly is the cheaper
  default.
- **Periodic source-weight recalibration.** Not a one-off: re-run
  `source-quality` every few weeks and check whether the 4A weights still hold
  as data accumulates. Treat the diagnostic as a standing practice.

### Tier 3 — worthwhile, lower leverage or small
- **Feed-health check.** Four configured sources produced zero mentions over six
  weeks (Worn & Wound, Urban Gentry, The Time Teller, WatchAdvisor — the last a
  known 404 from 2N). Flag dead/stale feeds — possibly a `source-quality` column
  or a digest line rather than a new surface.
- **`--write` flag for the schedule commands.** QoL: have `schedule` /
  `schedule-digest` / `schedule-discovery` save the plist directly instead of
  printing it to pipe through `sed`.
- **Per-maker momentum chart.** The third trend view, deferred out of 4F (which
  shipped overall mention volume + top-10 rank trajectories). A mentions-over-
  time sparkline on the maker detail page, scoped to one maker. Small, additive,
  no schema change — same SVG-geometry approach as the /trends charts.
- **Automated seed entry** (paste a paragraph → LLM extracts fields → append
  YAML). QoL, but `promote` already graduates discovered makers and the seed is
  fairly complete — low marginal value until adding many brand-new seeds.
- **GPHG multi-edition mention dedup.** Defensible either way; only if the
  3×-nomination inflation looks wrong with more data.
- **Saved explorer views / tag-coverage health view** (net-new, light).
  Bookmarkable filter combos, or a coverage-per-dimension view to steer tagging.
  Premature — filters re-enter fast.

### Considering (genuinely new, not yet committed)
- **LLM cost/usage logging per discovery run.** Now that weekly discovery (4B)
  spends Haiku credits, a tokens-/cost-per-run line keeps spend visible.
- **"This week's new discoveries" review surface**, distinct from `/triage` —
  though it may overlap the digest's new-discoveries section enough to skip.

### Conditional upgrades (don't build reactively)
- **Reddit legacy API** (vs the current `top/.rss?t=week`): richer per-post data
  (upvotes, comments), needs an application + days-to-weeks wait. Only if 2-3
  weeks of RSS shows specific coverage gaps it can't fill.
- **Broader model coverage** beyond GPHG nominees: LLM-extract model refs from
  RSS (noisier, would muddy the clean GPHG-only data) or per-brand scrapers
  (high maintenance, only for the most-tracked 10-20 makers). Use the current
  GPHG-only tier first and see whether gaps actually bother you.

### Parked (a decision, not a feature)
- **Public deployment** — the fork in the road; it re-tiers everything above.
  Parked for good reasons (hosting, abuse, maker-rights, cost). Decide
  deliberately before letting it drive the roadmap. (Also in "Open questions".)

## The bigger arc: discovery → engagement

Captured 2026-05-11. The current tool surfaces makers. The longer arc
is to help users *figure out which makers they'd be interested in*
and eventually to act on that — contact, relationship, becoming part
of a maker's story. Most of this arc has now shipped (see
Sequencing below); only Layer 3b — adaptive ranking — remains.
It is the headline Tier 1 item in the Roadmap.

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

- **1a — Schema + display: SHIPPED (3A).** Seven taste columns on
  Maker (migration 0006), a "Profile" panel on the detail page, a
  controlled vocabulary in `watch_app/taste.py` with validation, and
  30 well-known makers hand-tagged in the seed YAML.
- **1b — Bulk population: SHIPPED (3B).** `draft-taste` CLI walks
  untagged independents, asks Haiku for vocabulary-constrained tags
  (abstaining when unsure), and writes drafts into the seed YAML for
  human review. Piloted on 10, then run across the rest. Every draft
  reviewed before commit. Conglomerates skipped.
- **2 — Multi-filter explorer: SHIPPED (3O/3P).** The /explore UI lets you
  navigate by these dimensions, with similarity ("makers like X") built on
  `_similar_makers`.
- **3 — Reactions: CAPTURE SHIPPED (3V/3X).** Like/dislike per maker plus the
  `/triage` queue. Only the adaptive re-ranking (Layer 3b) is left — the arc's
  last piece. See the Roadmap.

Layers 1a/1b came in faster than the 4-6 week estimate because the
LLM-assisted draft + review workflow turned ~420 tag decisions into a
review pass rather than hand entry.

### Open design questions for when we get here

**Resolved in 3A/3B:**
- *Vocabulary stability* — shipped small as planned: 5-6 values per
  dimension, no "finishing-led/complication-led" (folded into a separate
  `complication_focus` dimension instead), no "conglomerate-independent"
  maker_type. Added `movement_origin` (in-house/modified-base/stock-base)
  as a seventh dimension — it turned out to be a strong discriminator for
  indies. Final vocabulary is the controlled set in `watch_app/taste.py`.
- *Subjective vs objective* — didn't over-annotate. The Profile panel
  carries one plain footnote ("aesthetic and type are editorial judgment,
  not fact") rather than per-tag `[claude]` markers.
- *Public vs personal* — kept multi-tag for the two contested dimensions
  (`aesthetic_tags`, `complication_focus`); the rest are single-value.

Still open / surfaced during the work:
- *Vocabulary gaps.* The bulk pass surfaced at least one real gap:
  resonance / skeletonization (Armin Strom's signature) can't be
  expressed. One data point so far — expand only if it recurs.
- The original three questions, for reference:

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

### Search, filters, pagination
Top-20 fits on screen. Don't add these until the dataset makes
browsing painful — which it isn't.

### Daily default-on LLM extraction
No longer a blanket "no" to LLM scanning — a *weekly* LLM discovery scan now
runs (4B), and a cheap daily variant is a Tier-2 Roadmap candidate (the
skip-seen-URL guard). What's parked is specifically making *every daily* scan
LLM-backed: it re-pays Haiku for the whole feed window each run for marginal
value. The daily job stays rule-based.

(Charts on the dashboard moved OUT of this section — the data gate is met, it's
now a Tier-1 Roadmap candidate. Public deployment lives in the Roadmap's
"Parked" bucket and "Open questions".)

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

**Are the prestige weights calibrated right?** RESOLVED (4A). The
`source-quality` diagnostic (3Y/3Z) validated the Phase-1 guesses against six
weeks of data, and 4A acted on it (Hodinkee up; low-discovery channels down).
Now a periodic recalibration *practice* (Roadmap, Tier 2), not an open question.

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
- 3A  Taste dimensions (Layer 1a). Seven taste columns on Maker (migration
      0006): aesthetic_tags + complication_focus (multi-tag, JSON),
      maker_type, price_band, output_scale, distribution, movement_origin
      (single, nullable). New watch_app/taste.py holds the controlled
      vocabulary + sanitize_taste + validate_taste_fields (single source of
      truth). Seed loader reads/validates/writes the fields; a "Profile"
      panel renders them on the maker detail page. 30 well-known makers
      hand-tagged in the seed. Taste is descriptive — it does not feed
      scoring.
- 3B  Taste drafting tool (Layer 1b). `cli draft-taste`
      (dry-run / --commit / --limit / --only) walks untagged independents,
      calls Haiku for the seven dimensions constrained to the vocabulary
      (null-on-uncertain, biased to abstain on output/distribution/movement),
      sanitizes against the vocab, and writes drafts into the seed YAML for
      human review. Piloted on 10 recognizable makers, then run across the
      rest. Writes to the YAML (curated source of truth), deliberately NOT
      the DB enrichment registry — vocab-validation confirms a tag is valid,
      not correct, so taste wants human review.
- 3C  Cleanup. Fixed three stale YouTube retry tests (they asserted pre-2V
      retry-on-404; rewritten so one asserts 404 short-circuits and the
      other two exercise retry/backoff with a 5xx). Pinned httpx to 0.27.2 —
      anthropic 0.39.0 passes a `proxies=` arg that httpx 0.28 removed, which
      broke every LLM call on a clean install. Full suite green (372) after.
- 3D  is_independent data-quality fix. Glashütte Original (Swatch Group),
      Arnold & Son and Angelus (both Citizen, via La Joux-Perret) had no
      is_independent key, so they defaulted to independent and got
      taste-tagged in the 1b run. Marked them false and stripped the taste
      tags. Ulysse Nardin was checked and kept independent (Kering sold
      Sowind to management in 2022). Shipped as an idempotent in-place
      script, after the original full-file version would have clobbered
      committed draft work.
- 3E  Documentation refresh — README + NOTES updated for the 3A-3D taste
      work: feature inventory, draft-taste CLI, a taste-dimensions section,
      project-layout additions, test count 326→372, Layers 1a/1b marked
      shipped, design questions resolved, this chunk log. Doc-only.
- 3F  Taste tagging for auto-discovered makers. Closes the gap where a
      discovered maker (e.g. Barrelhand) got the factual fields but no
      taste/character tags, because draft-taste (3B) only walks the seed
      YAML and the factual on-scan enricher's registry doesn't include taste.
      New `taste_enriched_at` column (migration 0007) is the once-per-maker
      gate, separate from `enriched_at` so already-enriched makers still get
      taste, and stamped even on abstention so the heavy-abstaining LLM isn't
      re-queried every scan. `auto_tag_taste_for_discovered()` selects
      non-seed independents with taste_enriched_at IS NULL, calls the existing
      cached `lookup_maker_taste`, sanitizes to vocabulary, writes with the
      "auto" enriched_fields marker, capped at 25/scan; wired as a new `taste`
      phase in run_scan after enrichment. `cli enrich --taste` adds a
      dry-run/commit backfill path for makers discovered before this. Scoped
      to discovered makers only — seed taste stays curated in the YAML. The
      review/edit UI (originally paired as the next half) is deferred to its
      own chunk.
- 3G  Context-aware taste prompt + retag. The 3F tagger fed Haiku only a bare
      name, so genuinely niche makers (Barrelhand, Beaubleu, Spaceone…)
      abstained. `lookup_maker_taste` now takes an optional `context` block
      built from the maker's recent mention titles + excerpts (most-recent
      first, deduped, capped) — for Barrelhand that surfaces "San Francisco
      mechanical engineer… next-generation tool watch… mission-grade space
      watch," more than enough to tag. Prompt version bumped v1→v2 (context is
      in the cache key). Because 3F already stamped `taste_enriched_at` on the
      abstainers, a better prompt wouldn't reach them on the normal gate, so
      `cli enrich --taste --retag` re-opens makers that were attempted but
      left untagged (skips anything already tagged, so no clobber). On-scan
      tagging keeps the strict `taste_enriched_at IS NULL` gate; retag is
      CLI-only and explicit.
- 3H  Review/edit UI (admin). New `/review` page (FastAPI route + Jinja
      template, matching the existing stone/amber style) lists every
      discovered non-seed maker with its seven taste dimensions plus the
      factual fields, all editable: checkboxes for the multi-value dims,
      selects for the single-value ones, text/number/checkbox for
      country/founded/website/independence. POST `/review/maker/{id}`
      validates against the vocabulary, then overwrites (human is
      authoritative — emptying a field clears it), drops the edited fields'
      "auto" markers, and stamps taste_enriched_at. Gated by a new
      `admin_mode` setting (WATCH_APP_ADMIN, default off) — routes 404 when
      off, and the Discoveries page only shows the entry link when on. This is
      the manual-tagging home for the abstainers (Barrelhand et al.) the
      auto-tagger can't reach. Seed makers excluded (their taste lives in the
      YAML). No schema change.
- 3I  Test-suite speedup. `make test` was 10-29 min locally because `run_scan`
      (in test_pipeline / test_scan_runs / test_orchestrator_with_rss) made
      real Anthropic enrichment+taste calls for every unenriched seed maker
      and real `http.get` calls for DummySource's example.com URLs (serialized
      by the 1.5s/host rate limiter). Two autouse conftest fixtures now stub
      the enrichment/taste lookups (abstain) and block real `watch_app.http.get`
      (RuntimeError), with a `real_http` marker opt-out for test_http.py.
      pytest.ini registers the marker and pins the pytest-asyncio loop scope to
      silence its deprecation warning. Full suite: ~30s, 399 passing, zero real
      external calls (so no more API spend per run). No production code touched.
- 3J  Taste explorer (Layer 2a). New `/explore` page: faceted filtering over
      every maker carrying ≥1 taste tag (seed + discovered), OR within a
      dimension and AND across, with static per-option counts. Matches are
      ranked by a selectable scoring preset (reuses fetch_top_makers), so you
      can ask for e.g. "rising stars among classical solo-watchmakers." All
      seven dimensions render as checkbox groups (the multi/single split only
      affects match logic, not the UI); the panel + preset live in one HTMX
      form that re-fetches the results fragment on any change. Filtering and
      ranking run in Python over the ~160-maker universe — no SQLite JSON
      queries. Untagged makers are absent by design. Nav link added.
      Similarity ("makers like this") is the separate Layer 2b chunk.
- 3K  "Makers like this" similarity (Layer 2b). `taste.similarity(a, b)` scores
      two makers' taste as weighted overlap over the dimensions both have
      populated (normalized by those weights, so sparse makers aren't
      penalized; None when they share nothing). Jaccard on the multi-value
      dimensions, exact match on single-value (no ordinal adjacency yet).
      Weights lean on character — aesthetic_tags / maker_type /
      complication_focus / movement_origin at 2.0, price_band / output_scale /
      distribution at 1.0 (in `SIMILARITY_WEIGHTS`, tunable). A "Similar makers"
      section on the detail page shows the top matches with a % and the shared
      tags that drove them. Real-data check: Dufour → Raúl Pagès, Roger Smith,
      Akrivia, Strehler — exactly right. Pure function in taste.py; ~160-maker
      pairwise compute is trivial, no precomputation.
- 3L  Promote a discovery to seed. `extraction/promote.py` serializes a
      discovered maker's curated fields + populated taste into a new
      known_makers.yaml entry (matching the hand-curated style; null dims
      omitted so the loader's overwrite is a no-op) and appends it under a
      trailing "# --- Promoted from discovery ---" section, reusing the
      line-based write so existing formatting/comments survive. Validates the
      whole file re-parses and all taste is in-vocabulary before writing — no
      half-entry on failure. Guardrails: hard-block exact canonical/alias
      collision (also makes re-promotion idempotent), confirm-required on a
      rapidfuzz WRatio near-match (≥88, `--force` to override), soft-warn if
      untagged. Exposed as `cli promote <id|name>` (dry-run/--commit/--force)
      and a per-maker "Promote" button in /review (admin; same logic, 409 +
      CLI hint on blocker/near-match). After promotion the maker reconciles
      onto its existing DB row on the next scan and leaves the discovered
      views; further curation is via the YAML. No schema change.
- 3M  Per-source detail page. New `/sources/{id}` drill-down reached from the
      source-contributions table on /methodology (added source_id to the
      SourceContribution dataclass to link). `build_source_detail()` in
      methodology.py reuses the existing first-surfaced subquery (earliest
      discovered_at per maker, MIN(source_id) tie-break) scoped to one source,
      plus per-source totals, top makers by mention count, and recent mentions
      (titles + links only, no excerpt reproduction). Shows source metadata
      (category, prestige weight, active, last scraped). Read-only — weight
      tuning stays a scoring-config concern. No schema change. Next: resolver
      robustness (affix normalization + merge/unalias CLI + dupes report).
- 3N  Resolver robustness (prevent + remedy + observe). resolver._normalize now
      also removes periods and strips generic maker affixes (leading
      atelier/manufacture/maison/montres/horlogerie; trailing &cie/&co/sa/ag/
      gmbh/inc/ltd/llc/watches/watchmaking/watchcraft) — matching-only, never
      rewrites a stored name. This pulls apart the &Co./Atelier false-positive
      clusters (which sat at WRatio 86, one bad extraction from the 90 auto-
      merge line) and lands true variants (Atelier Akrivia~Akrivia, FP~F.P.) at
      the exact step. Audited the real seed: zero new cross-maker collisions.
      The entities normalizer (literal text matching) intentionally stays
      simpler; the resolver one is now a strict refinement, so it still matches
      everything extraction produces — updated test_hyphen_normalization to lock
      the shared core + the intended divergence. Fuzzy auto-aliases are now
      logged. New extraction/maker_admin.py + CLI: `dupes` (near-duplicate
      report), `merge <from> <into>` (reassign mentions/watch-models with
      (source,url) de-dup, fold aliases, drop derived Signal/Score rows, delete
      the row; dry-run default; blocks merging FROM a seed maker), `unalias`
      (remove a bad alias; warns on seed makers since the loader re-adds YAML
      aliases). No schema change.
- 3O  Maker suppression (soft hide + hard delete). New is_suppressed +
      suppressed_reason columns (migration 0008, server_default false so
      existing rows backfill). `extraction/maker_admin.py` gains
      suppress_maker / unsuppress_maker / list_suppressed (dry-run default,
      idempotent, reversible) and delete_maker (hard: drops the maker +
      mentions + watch_models + signals + scores; blocks seed makers, which
      the loader would re-create). CLI: `suppress` / `unsuppress` /
      `suppressed` / `delete`. A suppressed maker drops out of /discoveries,
      score rankings (fetch_top_makers), /comparison, and /review, but the row
      survives so the resolver re-matches (not re-creates) the name on later
      scans — a one-off article can't resurrect it, and unsuppress restores
      full history. Suppress for recurring names; delete only for a genuinely
      one-off mention that's aged out of the feed. The remedy for the
      not-really-trackable discovered names (see brand-validation findings).
- 3P  Time-window control on /explore. A "Window" filter in the sidebar:
      length (All-time / 3d / 7d / 14d / 30d) plus a basis toggle — Mentioned
      (most recent mention's published_at, else discovered_at) or Discovered
      (Maker.first_seen_at). All-time is the default, so the page behaves as
      before until you pick a window. The window narrows the tagged universe
      first, so facet counts and the total reflect it. The cutoff comparison
      runs in SQL (the fetch_rank_changes approach) because SQLite returns
      naive datetimes while a Python cutoff is tz-aware. New radios slot into
      the existing HTMX change-trigger flow; no JS. No schema change.
- 3Q  Suppressed makers excluded from enrichment + velocity regression test
      (this session's "Chunk A"). The on-scan enrichment and taste-tagging
      candidate queries (`iter_unenriched_makers`,
      `iter_untagged_discovered_independents`) now filter `is_suppressed IS NOT
      TRUE`, so a hidden maker stops costing Haiku calls; the exclusion is
      reversible (unsuppressing leaves enriched_at/taste_enriched_at NULL, so
      the maker becomes a candidate again). Also confirmed the velocity
      published_at fix was already in code and added a regression test pinning
      the precedence. No schema change.
- 3R  Personal relationship state + notes (this session's "Chunk B"). New
      `relationship_state` (target / watching / known / pass) + `notes` columns
      on Maker (migration 0009, both nullable). ORTHOGONAL to suppression:
      state captures engagement posture toward a *real* maker and does not
      affect visibility. Vocabulary + a set_relationship_state helper live in
      maker_admin.py. Set both from the detail page's "Your status" form
      (POST /makers/{id}/state, not admin-gated); a small state badge renders
      on the dashboard / explorer / discoveries rows (shared `_state_badge`
      macro); filterable on the dashboard ("Your list" links) and the /explore
      sidebar. The dashboard filter is a ranking view, so a marked-but-unscored
      maker won't appear there — noted on the page and in code.
- 3S  Weekly "what changed" digest. New `scoring/digest.py` with a shared
      `compute_weekly_digest(window_days, preset)` returning three sections:
      new discoveries (non-seed makers first seen in the window — via a new
      optional `since=` arg on fetch_discovered_makers), movers (straight from
      the fetch_rank_changes / movers engine), and "on your list" — watched or
      target makers that had activity in the window (new mentions, a rank move,
      or being newly discovered); stable no-activity makers are omitted so the
      section reads as an alert. Two surfaces over the one function: a `digest`
      CLI command (`--days`, `--preset`, `--markdown` for portable export) and
      a `/digest` page (first in the nav as "What changed"). `render_digest_
      markdown` is the export renderer. "In the window" = entered the data in
      the window (first_seen_at / mention discovered_at), deliberately distinct
      from velocity's published_at basis (documented in the module). Thin-
      history (<window of score baseline) is flagged on both surfaces. All
      window comparisons run in SQL — SQLite returns naive datetimes, so
      "newly discovered" is derived from the SQL-filtered id set, not a Python
      compare. No schema change.
- 3T  Digest email delivery. New `notify/` package: `notify/email.py` renders a
      summarized, inline-styled HTML body (leads with "on your list", caps
      discoveries/movers at `--cap`, default 5, with a "+N more" pointer) and a
      full self-contained styled HTML attachment, assembles a multipart message
      (markdown text fallback + HTML alternative + HTML attachment), and sends
      over stdlib smtplib. `send-digest` CLI command: dry-run by default (writes
      body + attachment to `--out`/data dir, sends nothing), `--send` to deliver;
      `--days`/`--preset`/`--to`/`--cap`. SMTP creds + recipient come from new
      Settings fields (WATCH_APP_SMTP_HOST/PORT/USER/PASSWORD/SSL, _DIGEST_FROM/
      _TO) — env only, never hardcoded; a real send with missing config reports
      exactly which vars are absent and sends nothing. SSL (465) default,
      STARTTLS (587) via flag. Send path isolated for test monkeypatching; no
      real mail in the suite. No schema change, no new dependencies. README has
      an "Email digest" setup section (Gmail app-password steps). Scheduler: 3U.
- 3U  Weekly digest scheduler. New `schedule-digest` CLI command, mirroring the
      existing `schedule` (daily scan) command: prints a per-user macOS
      LaunchAgent plist + the launchctl load/verify/disable steps, but on a
      WEEKLY `StartCalendarInterval` (Weekday + Hour + Minute) running
      `send-digest --send`, under its own label `com.user.watchapp.digest`
      (independent of the scan agent) logging to `data/digest.log`. Defaults to
      Monday 08:00 (just after the 7am scan, so data is fresh and the 7-day
      window lines up); `--day mon..sun`, `--hour`, `--minute` override, all
      validated. Output reminds you to confirm a manual `--send` works first,
      since the job runs the same command and reads SMTP creds from .env. To
      change the time later: regenerate the plist with new flags (or hand-edit
      the integers), then `launchctl unload && load` — the reload is what
      applies it. No schema change. (A `--write` flag to drop the plist
      straight into ~/Library/LaunchAgents and skip the copy/reload dance was
      offered as a follow-up, not yet built.) This completes the digest arc:
      compute/surfaces (3S) → email (3T) → schedule (3U).
- 3V  Taste reaction per maker — Layer 3's capture half. New `taste_reaction`
      (like / dislike / NULL) + `taste_reaction_at` columns on Maker (migration
      0010, both nullable). ORTHOGONAL to BOTH suppression and
      relationship_state: this is a like/dislike *taste* signal — the input the
      adaptive re-ranking (3b) will read — not an engagement posture and not a
      visibility control. Vocabulary (TASTE_REACTIONS) + a `set_taste_reaction`
      helper live in maker_admin.py beside the relationship-state ones; the
      timestamp restamps ONLY when the value actually changes, so a no-op
      re-save preserves the original react time (the "when did I first react"
      the data clock needs). Captured from a "Taste" control folded into the
      existing detail-page "Your status" form: POST /makers/{id}/state now sets
      state + notes + reaction in one round-trip (reaction is an optional Form
      field, so the prior state-only behavior is unchanged). 11 tests in
      tests/test_taste_reaction.py. Column was chosen over a reactions table for
      consistency with the existing per-maker state idiom; column→table stays a
      lossless backfill if 3b ever needs per-watch or per-dimension reactions.
      The `/triage` workflow (walk untriaged makers, react in one pass) is the
      deferred other half of this chunk.
- 3W  Per-maker scoring breakdown on the detail page. `build_worked_example`
      (the global #1, used by /methodology) was refactored to share an
      `_assemble_worked_example` core with a new
      `build_worked_example_for_maker(session, preset, maker_id)`. The detail
      page's old raw-only "Signal Breakdown" table is replaced by the full
      raw → z-score → weight → contribution → composite table for THAT maker
      under each preset, with its rank — so you can see why a given maker sits
      where it does, not just the global winner. Final score is computed the
      same way the ranker does (z-weighted sum → eligibility cap → independent
      multiplier); a new regression test asserts the shown total matches the
      stored Score within 1e-9 for every maker under every preset (the
      detail-page analogue of test_worked_example_final_score_matches_ranker).
      No schema change. (One web-route test that pinned the old raw signal-key
      display was updated to the new label + contribution display.)
- 3X  Triage flow — Layer 3a's workflow half (the deferred other half of 3V).
      A `/triage` queue of every untriaged maker (`taste_reaction` IS NULL),
      suppressed excluded; scored makers first in rank order under the default
      preset, then unscored newest-first (so high-signal calls sit on top while
      the unranked long tail stays reachable). Each card shows just enough to
      judge — name, country, maker_type, aesthetic tags, rank/score/mentions —
      with Like / Dislike buttons. Reacting POSTs to a NEW
      `POST /triage/{id}` (reuses `set_taste_reaction`) that returns an HTMX
      fragment swapping the card out + OOB-updating the header count; skipping
      is just leaving a card alone. Deliberately a separate route from
      `POST /makers/{id}/state` (that one 303-redirects, right for the detail
      form but wrong for an inline swap) and the queue offers no "clear" (use
      the detail page) so an empty reaction is a 400. NOT admin-gated —
      reacting is a personal-tool action. New nav item "Triage". Two new
      templates (`triage.html`, `_triage_reacted.html`); 12 tests in
      tests/test_triage.py. No schema change. This completes Layer 3a; with
      capture *and* the queue done, the data clock for 3b is now running.
- 3Y  Source-quality calibration diagnostic — the measurement half of the
      Tier-3 prestige-weight item, unblocked by 6 weeks of data (4/26–6/7).
      New `scoring/calibration.py` with `compute_source_quality(session,
      window_days=None)` returning, per source: total_mentions, distinct_makers,
      first_surfaced (discovered/non-seed makers it was first of any source to
      mention — same earliest-by-discovered_at + MIN(source_id) tie-break as
      build_source_contributions, so firsts sum to the discovered-maker count),
      first_ratio (= first_surfaced / total_mentions, the per-mention discovery
      yield — numerator is discovered-firsts, denominator is whole volume on
      purpose, since prestige_weight is a per-mention multiplier), and the
      current prestige_weight beside the evidence. Suppressed makers excluded
      from every count (seed-name match reuses discovery._load_seed_names).
      Surfaced as a `source-quality` CLI command (`--days N`, default all-time)
      rendering a Rich table + a caveats footer. DIAGNOSTIC ONLY — writes
      nothing, recommends no weights; the actual tuning is a deliberate
      follow-up (data before fix). No schema change, no scoring change. 7 tests
      in tests/test_calibration.py.
- 3Z  Calibration first-surfaced fix — same-session correction to 3Y. On real
      data every source read first=0: 3Y restricted first-surfaced to
      discovered (non-seed) makers, but the DB is seed-dominated (rule-based
      scans rarely mint new makers), so the column zeroed out. Root cause was
      conflating "the seed *entity* was known from day one" with "no source can
      be credited for surfacing it" — a source can still be the first to
      *mention* a curated maker over the scan window, which IS the calibration
      signal. Fix: first_surfaced now counts ALL non-suppressed makers (seed +
      discovered) and credits the earliest source by discovered_at — but
      AWARDS-category sources (GPHG) are excluded from the race, because the
      editions are bulk-ingested historically and would win first for nearly
      every maker they nominated by timing artifact (in the live data GPHG was
      ~54% of all firsts with 0 recent activity). Awards rows still show
      volume/breadth/weight, with "—" for first. Dropped the now-unused
      _load_seed_names import. tests/test_calibration.py rewritten for the new
      semantics (9 tests), incl. test_seed_makers_count_as_first_surfaced — the
      regression that pins this exact blind spot — and awards-exclusion +
      awards-only-maker cases. Still diagnostic-only; no schema/scoring change.
- 4A  Prestige-weight recalibration — the deliberate weight change the 3Y/3Z
      diagnostic was built to inform (the analysis half is done; this is the
      act-on-it half). Read the source-quality table over the 6 weeks: three
      sources form a clear top tier (SJX, Monochrome, Hodinkee, all ~18-19%
      first/mention) but Hodinkee was weighted 0.70 despite the MOST firsts (37)
      and broadest coverage; meanwhile several YouTube/community sources carried
      weights their discovery didn't earn (Bark and Jack at 0.90 with 0 firsts
      the worst). Changes: Hodinkee 0.70→0.95 (up); down — Bark and Jack
      0.90→0.45, Escapement & Watch 0.70→0.35 (28 mentions / 2 makers),
      Swiss Watch Gang 0.80→0.55, Watchfinder & Co. 0.50→0.35 (81 mentions /
      0 firsts), Teddy Baldassarre 0.60→0.45, r/horology + r/watchmaking
      0.60→0.45. Held: SJX 1.0 (anchor), Monochrome 0.90, Fratello 0.70,
      WatchBox 0.40, r/Watches 0.40, WatchCrunch 0.30, and deliberately Quill &
      Pad / Watches TV at 0.80 despite eye-catching yields (low-N noise). Edits
      are source class attrs (hodinkee.py, reddit.py) + youtube_channels.yaml;
      _upsert_source_row rewrites prestige_weight every scan, so the next scan
      propagates the weights AND rescores — no migration, no manual DB edit, no
      new command. No test changes (synthetic-source tests; the YAML range
      invariant max<=0.9 still holds). Caveats logged for the follow-up: First
      is keyed on discovered_at (scan-order can win same-day ties — Hodinkee's
      lead looks robust since its yield sits in line with peers, not above);
      awards weights (1.2) untouched (out of the first race by design); four
      silent sources (Worn & Wound, Urban Gentry, Time Teller, WatchAdvisor-404)
      are a feed-health item, not a weight one.
- 4B  Weekly LLM discovery scan scheduler. Confirmed the open thread: the
      launchd scan job runs rule-based (grep'd data/scan.log — every run
      use_llm=False), so the daily scan only re-ranks the seed and never mints
      new makers. Rather than flip the daily job to --use-llm (which re-pays
      Haiku for the whole feed window every day, since _ingest_mention has no
      skip-already-seen guard before the LLM call), added a SEPARATE weekly
      discovery job. New `schedule-discovery` CLI command mirrors
      `schedule-digest`: prints a launchd plist running `scan --use-llm
      --trigger discovery` weekly (default Mon 06:00, just before the 7am daily
      scan + 8am digest so new makers flow through that morning), with its own
      com.user.watchapp.discovery label and data/discovery.log. Daily
      rule-based scan unchanged — discovery complements, doesn't replace it.
      Cost is bounded to ~one feed-window of Haiku/week; the tradeoff is a
      stream of new noisy makers to triage (the curation layer exists for
      exactly this). 5 tests in tests/test_schedule_discovery.py. No schema
      change. README scheduling section + quick-ref updated.

      Deferred upgrade (was "option 3"): add a skip-already-ingested-URL guard
      before the LLM extract call so a DAILY --use-llm scan would be cheap AND
      complete (only pays for genuinely new articles). Do it if the weekly
      cadence proves too coarse — otherwise weekly is the cheaper default.
- 4C  NOTES.md roadmap consolidation (docs only — no code, no tests). Collapsed
      the five dated Update notes + the COMPLETED Tier 1-4 items + the all-shipped
      "Near-term candidates" / "Sitting in the backlog" sections into one lean
      `## Roadmap` (open items only; completed work stays in this log). Re-tiered
      by current leverage: Layer 3b and dashboard/trend charts as Tier 1; added
      this session's threads (daily-LLM skip-seen guard, feed-health check,
      periodic recalibration, `--write` flag) and a "Considering" bucket (LLM
      cost logging, weekly-discovery review surface). Fixed stale statuses: arc
      "Layer 2: NEXT" → SHIPPED (3O/3P) with only 3b remaining; "Explicitly NOT
      doing" reframed (dropped charts → now a candidate; default-on-LLM narrowed
      to the daily job since weekly discovery ships in 4B; public deployment
      deduped to one home); "Open questions" prestige-weights marked RESOLVED
      (4A). Deduped charts / public deployment / automated-seed-entry, each
      previously listed 2-3×.
- 4D  Suppress / delete a maker from the UI — the curation half of "stop
      tracking this." Surfaces the maker_admin operations (CLI since 3O) on the
      detail page, admin-gated like /review and promote (the personal-posture
      state/reaction form stays ungated). New POST routes suppress / unsuppress
      (reversible, 303 back to the page) and delete (303 to /discoveries, since
      the row is gone). The GET detail route takes ?confirm=delete to render an
      in-page confirm panel populated by a delete *dry-run* — the exact
      mention / watch-model / signal / score counts that would be removed —
      before anything commits; a seed maker shows the blocker (no confirm
      button) and the POST is a 409. _fetch_maker_detail now carries
      is_suppressed / suppressed_reason; a "Suppressed" header badge and an
      admin-only "Manage" section render the controls. No schema change
      (is_suppressed columns exist since 0008) and no new maker_admin logic —
      pure UI over the tested operations, so the dry-run/commit + seed guard are
      inherited, not reimplemented. New tests/test_maker_admin_ui.py (gating,
      control rendering, suppress/unsuppress toggle, confirm-panel counts,
      commit + redirect, seed-blocked). Suite 548 → 560. Merge from the UI (the
      near-duplicate + typeahead target picker) is the next chunk.
- 4E  Merge a maker from the UI — the remedy for "this isn't a new brand" (a
      duplicate or name variant of one already tracked), completing the
      maker-maintenance surface started in 4D. Surfaces merge_makers (CLI since
      3N) on the detail page, admin-gated. Two ways to pick the target: a
      "Likely duplicates" row built from find_near_duplicates filtered to this
      maker (the common near-name case, no typing), and an HTMX typeahead
      fallback over all makers — new GET /makers/{id}/merge/search returns a
      _merge_results.html fragment (case-insensitive substring over
      canonical_name + aliases, ≥2 chars, self excluded, seed makers included
      since they're valid *targets*). Both lead to the same in-page confirm
      panel (GET ?confirm=merge&into=…, a merge dry-run) showing what moves —
      mentions reassigned/deduped, watch models, dropped signal/score rows,
      aliases folded — before POST /makers/{id}/merge commits and 303-redirects
      to the surviving maker. Blockers (seed source, self-merge, missing target)
      surface as 409 and as the panel's blocked state. No schema change and no
      new maker_admin logic — pure UI over the tested operation, dry-run/commit
      and the seed-source guard inherited. find_near_duplicates is O(N²) over
      makers and runs per admin detail-page load; fine at the current DB size,
      revisit with a cap/threshold if it ever drags. test_maker_admin_ui.py
      extended (search gating + matching + self-exclusion + short-query, confirm
      panel, commit + alias-fold + mention-move + survivor redirect, self- and
      seed-blocked, suggestions render). Suite 560 → 571.
- 4F  Trends page (`/trends`) — the time-series visual layer NOTES had parked
      as Tier 1 until the data gate cleared (now 7 weeks, 4/26–6/14). Two
      hand-rolled server-side SVG charts, no JS and no new dependency:
      (1) mention volume over time and (2) rank trajectories for the current
      top 10 under the selected preset. Two new data functions in
      scoring/queries.py: `fetch_mention_volume` (daily buckets, gap-filled) and
      `fetch_rank_trajectories` (current top-N + their full stored Score
      history). Trajectories are a plain history read — `rank` is already stored
      per (maker, preset, computed_at), the same column /movers compares — so no
      re-scoring and no schema change. Geometry is computed in two pure router
      helpers (`_build_volume_chart`, `_build_trajectory_chart`) unit-tested
      directly for the divide-by-zero edges (single day, single snapshot,
      all-zero). Route mirrors /movers (preset-aware via `_resolve_preset`,
      graceful empty states); new `trends.html`; "Trends" nav link after Movers.
      Decision worth recording: volume counts NON-awards mentions by
      `discovered_at`, NOT `COALESCE(published_at, discovered_at)` as first
      scoped — GPHG awards write Mention rows with publication dates back to
      2023, which would have stretched the axis across years and buried the
      organic 5–25/day signal. discovered_at + awards-exclusion matches the
      source-quality diagnostic's basis and keeps the series bounded to the
      project's own lifetime; the chart labels the exclusion. Per-maker momentum
      (the third candidate view) deferred to a Tier 3 follow-up. 21 tests in
      tests/test_trends.py. Suite 571 → 592.
- 4G  Made the /trends charts interactive (vanilla JS, no new dependency). The
      4F charts were deliberately zero-JS server-rendered SVG; this adds a small
      `web/static/trends.js` (served via the existing /static mount) that does
      pure DOM glue over that SVG — no library, no recompute. Volume gets a
      hover tooltip with a vertical guide + point marker (nearest day by cursor
      x). Trajectories get hover-or-legend highlight (the hovered maker lifts,
      the rest dim to 0.12 — this is the fix for the 10-line tangle), a tooltip
      reading "<name> · #<rank> on <date>" off the nearest point on the hovered
      line, and click-a-swatch to toggle a line off. The data each tooltip needs
      (dates/counts/ranks/names) is emitted server-side: the two geometry
      helpers (`_build_volume_chart`, `_build_trajectory_chart`) now also return
      per-point dicts, dumped into `<script type="application/json">` blobs the
      JS parses. Markup: a `relative` wrapper + absolute tooltip div per chart,
      transparent wide "halo" polylines as easy hover targets, hidden
      marker/guide elements positioned by the JS. State (highlight/dim/hidden) is
      done via inline-style toggles + Tailwind's `.hidden`; no custom CSS and
      base.html untouched (script tag lives in trends.html). Degrades to the
      static 4F charts with JS off. Testing note worth recording: the suite has
      no browser/JS harness and adding one (Playwright/jsdom) is the kind of new
      dependency this project avoids, so the *interaction behavior* is NOT
      auto-tested — `node --check` covers JS syntax, and 6 new server-side tests
      cover the hooks the JS depends on (parseable data blobs, one halo + one
      swatch per line, tooltip containers, the geometry helpers' new point data,
      and that /static/trends.js is served). Verified the behavior by reasoning,
      not a browser test; flagged this to confirm before building. Suite 592 →
      598.
- 4H  Surfaced suppress / delete on the `/review` grid, so a discovered maker
      can be curated without opening its detail page. 4D put these on the detail
      page; this is the same maker_admin operations (suppress_maker /
      delete_maker), reached from review. Each card gets a confirm-guarded
      Suppress button (POST /review/suppress/{id}) and a Delete… link. No new
      operation logic — the dry-run/commit and seed guard are inherited; these
      routes differ from the detail-page ones only in redirect target, both 303
      back to /review (vs the detail page's /makers/{id} and /discoveries) so
      you stay on the grid. Suppress drops the maker off the grid for free,
      since _fetch_discovered_for_review already filters is_suppressed. Delete
      goes through an in-CARD confirm panel: the Delete… link is GET
      /review?confirm=delete&maker={id}, which runs a delete dry-run and renders
      the same red panel the detail page uses (exact mention / watch-model /
      signal / score counts) inside that maker's card, with a Confirm delete
      that POSTs /review/delete/{id}; Cancel is a link back to /review. Per-card
      (not top-of-page) placement was chosen so the confirm sits with the maker
      it names. One asymmetry worth recording: seed makers never appear on
      /review (filtered), so the panel's blocker branch is unreachable through
      the UI there — the POST route still 409s as a backstop. The review GET
      route gained confirm/maker query params + a delete_preview; no schema
      change, no new dependency. 9 tests in tests/test_review_maintenance.py
      (gating, controls render, suppress + drop-off, confirm-panel counts,
      commit + redirect, seed 409). Suite 598 → 607.

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

## Findings from the taste-dimensions work (3A-3D)

**The draft → review workflow held up.** The Haiku bulk pass tagged ~46 of
50 untagged independents; the 4 blanks were the model correctly abstaining on
makers too obscure to know. Accessible brands came out right (Christopher
Ward → microbrand / mid / 500-plus / direct / stock-base), and J.N. Shapiro
correctly abstained on movement_origin (mid-transition to an in-house
caliber). Turning ~420 tag decisions into a review pass was the right call.

**The seed silently defaults unknown brands to independent.** Glashütte
Original, Arnold & Son, and Angelus had no `is_independent` key, so
`get("is_independent", True)` treated them as indies — they slipped into the
discovery set and got taste-tagged before we caught it (3D). The same is true
of any group-owned brand added without the explicit flag. Worth a periodic
audit: list seed entries where the key is missing and sanity-check the
larger/older names against current ownership. Ownership changes — Ulysse
Nardin is independent again (2022 management buyout from Kering), so it was
kept; verify rather than assume.

**Non-watch / non-brand entries in the seed.** The bulk pass surfaced two
entries that probably shouldn't be discovery targets at all: Kikuo Ibe (the
G-Shock inventor — a Casio engineer, not an eponymous brand) and L'Épée 1839
(a clock manufacturer, not wristwatches; M.A.D. Editions and Miki Eleta are
adjacent edge cases). Left in for now — these are "should this be a maker"
questions, separate from taste or independence. Candidate for a small pruning
pass.

**One vocabulary gap so far.** Armin Strom's signature is resonance /
skeletonization, which the `complication_focus` vocabulary can't express. One
data point — hold the vocabulary stable and expand only if it recurs across
more makers.

**Dependency incompatibility (fixed in 3C).** `anthropic==0.39.0` passes a
`proxies=` argument that `httpx==0.28.0` removed, so a clean install broke on
the first LLM call. Pinned httpx to 0.27.2. The cleaner long-term fix is to
bump `anthropic` to a current release that supports httpx 0.28+ — a larger
change with its own surface area, left for when there's reason to touch the
LLM client.

**Test suite was slow on the dev machine — FIXED in 3I.** `make test` had
been taking 10-29 minutes locally. The cause wasn't unmocked source tests (those
mock their own `http_get`); it was `run_scan` itself. Its enrichment and taste
phases run on every scan regardless of `use_llm`, so each scan test made real,
billable Anthropic calls for every unenriched seed maker — and the GPHG
detail-fetch step (`_fetch_gphg_models`) issued real `http.get` calls for the
DummySource's `example.com` URLs, serialized by the 1.5s/host rate limiter.
Fixed with two autouse conftest guards: one stubs the enrichment/taste lookups
to abstain, the other blocks real `watch_app.http.get` (with a `real_http`
marker opt-out for the one test that exercises the real client). Suite now runs
in ~30s and makes no real external calls. `pytest --durations` is the way to
catch any future regression.

## Brand-validation findings (3O, 2026-06-02)

Reviewed four obscure entries to decide keep / suppress / delete. The point
that recurred: "showing up in the data" isn't the same as "a real, active,
independent wristwatch maker worth tracking," and the determination is partly a
judgment call. Suppression (3O) is now the remedy — reversible, and re-matching
means a one-off article can't resurrect a name.

- **Ferdinand Berthoud (139) — KEEP.** Real, active, GPHG-winning (Chopard-
  backed Chronométrie). The "doesn't exist anymore" premise was wrong. Do not
  suppress.
- **Maison Alcée (150) — scope call.** Real and independent, but makes DIY
  desk-clock *kits*, not wristwatches — hence the "toolkit" impression. Keep or
  suppress depending on how strictly the tool is wristwatch-only.
- **Victor Kullberg (158) — suppress/delete.** Defunct 19th-century maker,
  surfacing only via auction/vintage coverage (the recent SJX piece). Clear
  candidate.
- **Matthew Norman (151) — suppress/delete.** Swiza-line quartz/clock brand,
  out of scope. Clear candidate.

Open theme worth a future feature: an LLM-assisted "is this an active
independent wristwatch maker?" check at discovery (or a flag in /review) would
cut this manual vetting — scoped to inform, not auto-suppress.

## Potential future scoring refinements (not urgent)

- **Mention deduplication across editions.** A brand nominated by GPHG
  in 2023, 2024, 2025 currently counts as 3 source contributions.
  Defensible (each year is a separate jury making a separate decision)
  but worth thinking about if the inflation looks wrong over more data.
- **`discovered_at` vs `published_at` in velocity calculations. — RESOLVED.**
  `compute_velocity_90d` (and `first_seen_recency`) coalesce `published_at` →
  `discovered_at`, so historical articles ingested on day 1 no longer count as
  "recent." Regression test pinning the precedence added in 3Q.
- **Per-source prestige tuning based on actual data.** After 4–6 weeks,
  could analyze which sources surface makers that *also* appear in GPHG
  and use that overlap as evidence for prestige weight. Right now
  weights are best guesses.
