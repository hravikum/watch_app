"""Post-discovery enrichment of Maker rows.

When the resolver auto-discovers a maker from an article mention, the
resulting Maker row has no website, country, or other metadata. This
module fills the gaps via LLM lookup with field-specific verification.

## Architecture (refactored in 2T)

Each enrichable field is described by an `EnrichmentField` entry in
the FIELDS registry. Adding a new field means adding one entry plus
implementing the lookup function in `llm/client.py`. The orchestration
code is field-agnostic.

Currently registered: website, country, is_independent.

## Verification strategies

Different fields have different ways to verify the LLM's output:

- **website**: HTTP-resolve. If the URL doesn't return 2xx/3xx, reject.
  Also reject blocked aggregator/dealer domains.
- **country**: known-list match. We tell Haiku what countries we accept
  and reject anything outside that list. Done in `llm/client.py` before
  the value gets back to this module.
- **is_independent**: strict YES/NO/NONE prompt. Only YES/NO commit;
  NONE skips. Done in `llm/client.py` before the value gets back here.

The "never overwrite existing data" rule applies to all fields:
enrichment only writes when the current value is None. Separately, a
maker you've saved on /review (``reviewed_at`` set, chunk 4N) is skipped
entirely — on both the factual and taste paths.

## Default behavior

Three modes:

- **Dry-run** (default for `cli enrich`): compute proposals, print
  them, don't commit. User reviews and re-runs with --commit.
- **Manual commit** (`cli enrich --commit`): compute and write all
  committable proposals.
- **Automatic on-scan** (2T): orchestrator calls
  `auto_enrich_unenriched_makers(session, limit=25)` after scoring
  completes. No dry-run; commits immediately. Cap limits cost from
  runaway scans.

## Cost ceiling

~$0.003 per maker per field (one Haiku call, ~600 tokens). With 3
fields and the 25-maker cap, automatic enrichment per scan tops out
around $0.20 even in pathological cases. Realistic per-scan cost is
under $0.01 once initial backfill is done.

## Taste tagging (chunk 3F)

Separate from the factual field registry above, `auto_tag_taste_for_discovered`
fills the seven taste/character dimensions for auto-discovered independents.
It runs on the orchestrator's taste phase and via `cli enrich --taste`. See
the "Taste tagging" section near the bottom of this module for why it has its
own `taste_enriched_at` gate rather than riding the `enriched_at` path.

## Web-researched audit (chunk 4O)

`cli enrich --audit` researches each discovered maker in the /review queue
with web search: a scope verdict, the brand a person / partial / model name
belongs to, a pre-launch flag, and drafts for every field on the card, with
the pages relied on. CLI-only — not on scan. See the section at the bottom.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from watch_app import taste
from watch_app.extraction.resolver import _normalize
from watch_app.http import get as http_get
from watch_app.llm.client import (
    AUDIT_VERDICTS,
    KNOWN_WATCHMAKING_COUNTRIES,
    lookup_maker_audit,
    lookup_maker_country,
    lookup_maker_independence,
    lookup_maker_taste,
    lookup_maker_website,
)
from watch_app.storage.models import Maker


logger = logging.getLogger(__name__)


#: Domains where a URL is almost certainly NOT a maker's official website.
#: Used by the website verifier.
BLOCKED_DOMAINS = frozenset(
    [
        "chrono24.com",
        "watchcharts.com",
        "watchcollecting.com",
        "watchuseek.com",
        "reddit.com",
        "instagram.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "tiktok.com",
        "wikipedia.org",
        "wikidata.org",
        "hodinkee.com",
        "ablogtowatch.com",
        "watchesbysjx.com",
        "monochrome-watches.com",
        "fratellowatches.com",
        "quillandpad.com",
        "worldtempus.com",
    ]
)


# ---------- Per-field configuration ----------


@dataclass
class EnrichmentField:
    """Configuration for one enrichable field.

    `name` is the Maker attribute name (e.g., 'website').
    `lookup_fn(session, maker)` returns the proposed value or None.
    `verify_fn(value)` returns (is_acceptable, reason) — if not acceptable,
        the reason is a short string for logging/display.
    `auto_enrich` is True if this field gets enriched on the automatic
        on-scan path. False means manual `cli enrich` only.
    """

    name: str
    lookup_fn: Callable[[Session, Maker], Any]
    verify_fn: Callable[[Any], tuple[bool, str | None]]
    auto_enrich: bool


# ---------- Verifiers ----------


def _is_acceptable_url(url: str) -> str | None:
    """Format-only URL acceptability check (no HTTP call).

    Returns None if acceptable, or a rejection reason string. Kept as a
    public-ish helper because the format checks (scheme, hostname,
    blocked-domain) are useful independently of HTTP verification — and
    because existing tests (from 2Q) use this signature.

    The full website verifier (including HTTP-resolve) is `_verify_website`.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unexpected scheme {parsed.scheme!r}"
    host = (parsed.hostname or "").lower()
    if not host:
        return "missing hostname"
    host_no_www = host[4:] if host.startswith("www.") else host
    if host_no_www in BLOCKED_DOMAINS:
        return f"blocked domain {host_no_www}"
    return None


def _verify_website(url: str | None) -> tuple[bool, str | None]:
    """Validate a proposed website URL. URL must be well-formed, not in
    blocked domains, and actually resolve via HTTP."""
    if url is None:
        return False, "LLM returned NONE"
    # Format check
    reason = _is_acceptable_url(url)
    if reason is not None:
        return False, reason
    # HTTP-resolve check
    try:
        response = http_get(url)
        if response.status_code >= 400:
            return False, f"URL returned HTTP {response.status_code}"
    except Exception as exc:
        return False, f"URL did not resolve: {exc}"
    return True, None


def _verify_country(country: str | None) -> tuple[bool, str | None]:
    """Validate a proposed country. The LLM client already filters by the
    known-list so by the time we see it, it's either a known country or
    None. This verifier mostly just rejects None."""
    if country is None:
        return False, "LLM returned NONE"
    if not isinstance(country, str) or not country.strip():
        return False, "empty value"
    return True, None


def _verify_independence(is_indep: bool | None) -> tuple[bool, str | None]:
    """Validate a proposed independence flag. The LLM client returns
    True/False/None — we accept the first two, reject None."""
    if is_indep is None:
        return False, "LLM returned NONE"
    if not isinstance(is_indep, bool):
        return False, f"unexpected type {type(is_indep).__name__}"
    return True, None


# ---------- Lookup adapters ----------
# These wrap the LLM client functions to match the EnrichmentField.lookup_fn
# signature `(session, maker) -> Any`. Lets the field config stay clean
# and adds defensive context where needed.


def _lookup_website_for_maker(session: Session, maker: Maker) -> str | None:
    return lookup_maker_website(session, maker.canonical_name, country=maker.country)


def _lookup_country_for_maker(session: Session, maker: Maker) -> str | None:
    return lookup_maker_country(session, maker.canonical_name)


def _lookup_independence_for_maker(session: Session, maker: Maker) -> bool | None:
    return lookup_maker_independence(session, maker.canonical_name, country=maker.country)


# ---------- Field registry ----------


FIELDS: list[EnrichmentField] = [
    EnrichmentField(
        name="website",
        lookup_fn=_lookup_website_for_maker,
        verify_fn=_verify_website,
        auto_enrich=True,
    ),
    EnrichmentField(
        name="country",
        lookup_fn=_lookup_country_for_maker,
        verify_fn=_verify_country,
        auto_enrich=True,
    ),
    EnrichmentField(
        name="is_independent",
        lookup_fn=_lookup_independence_for_maker,
        verify_fn=_verify_independence,
        auto_enrich=True,
    ),
    # Future: founded_year, with a different verify_fn (e.g., sanity-range
    # check 1800-current-year + LLM confidence threshold). Not shipped in
    # 2T because we don't have a robust verification mechanism for it.
]


def _field_by_name(name: str) -> EnrichmentField | None:
    """Look up a field config by name."""
    for f in FIELDS:
        if f.name == name:
            return f
    return None


# ---------- Per-field proposals ----------


@dataclass
class FieldProposal:
    """One proposed change for a single (maker, field) pair."""

    field_name: str
    proposed_value: Any
    verified: bool
    rejection_reason: str | None

    @property
    def is_committable(self) -> bool:
        return self.verified and self.rejection_reason is None


class EnrichmentProposal:
    """All proposed changes for one maker. May span multiple fields.

    Backwards-compatible with the original 2Q shape: callers can still
    pass `proposed_url`, `url_resolved`, and `rejection_reason` as
    keyword arguments to the constructor, and read them back as
    properties. Tests written against the 2Q shape continue to work.
    """

    def __init__(
        self,
        maker_id: int,
        canonical_name: str,
        country: str | None = None,
        field_proposals: list[FieldProposal] | None = None,
        # 2Q back-compat kwargs:
        proposed_url: str | None = None,
        url_resolved: bool = False,
        rejection_reason: str | None = None,
    ):
        self.maker_id = maker_id
        self.canonical_name = canonical_name
        self.country = country
        if field_proposals is not None:
            self.field_proposals = list(field_proposals)
        elif proposed_url is not None or rejection_reason is not None:
            # 2Q-style construction: synthesize one FieldProposal for website.
            self.field_proposals = [
                FieldProposal(
                    field_name="website",
                    proposed_value=proposed_url,
                    verified=url_resolved and rejection_reason is None,
                    rejection_reason=rejection_reason,
                )
            ]
        else:
            self.field_proposals = []

    @property
    def committable_proposals(self) -> list[FieldProposal]:
        return [p for p in self.field_proposals if p.is_committable]

    @property
    def is_committable(self) -> bool:
        """True if at least one field is committable. Preserved from 2Q."""
        return any(p.is_committable for p in self.field_proposals)

    # ---------- Back-compat properties for 2Q-shaped tests ----------

    @property
    def proposed_url(self) -> str | None:
        for p in self.field_proposals:
            if p.field_name == "website":
                return p.proposed_value
        return None

    @property
    def url_resolved(self) -> bool:
        for p in self.field_proposals:
            if p.field_name == "website":
                return p.verified
        return False

    @property
    def rejection_reason(self) -> str | None:
        """For 2Q compat: returns the website field's rejection reason
        if any. New code should look at field_proposals directly."""
        for p in self.field_proposals:
            if p.field_name == "website":
                return p.rejection_reason
        return None


# ---------- Public API ----------


def iter_unenriched_makers(
    session: Session, only_auto_enrichable: bool = False
) -> Iterator[Maker]:
    """Yield Maker rows where enrichment has not yet been run.

    Ordered by canonical_name for stable dry-run output.

    `only_auto_enrichable`: if True, restrict to rows where
    enriched_at IS NULL (the automatic-on-scan filter). If False
    (default), use the 2Q definition: at least one of the enrichable
    fields is None.

    Default behavior preserves the 2Q semantics so the CLI still works
    the same way.

    Suppressed makers are excluded on both paths: a maker you've hidden
    shouldn't consume LLM spend. The exclusion is reversible — unsuppressing
    leaves enriched_at NULL, so the maker becomes a candidate again.

    Reviewed makers (saved on /review, chunk 4N) are excluded on both paths
    too: a human has signed off on every field. Never-overwrite alone doesn't
    protect them — saving drops the "auto" marker that is is_independent's
    only guard, and a field the reviewer cleared reads as missing — so the
    2Q path would otherwise undo their independence call and refill cleared
    fields from the LLM cache.
    """
    if only_auto_enrichable:
        yield from (
            session.query(Maker)
            .filter(Maker.enriched_at.is_(None))
            .filter(Maker.is_suppressed.isnot(True))
            .filter(Maker.reviewed_at.is_(None))
            .order_by(Maker.canonical_name)
            .all()
        )
        return

    # 2Q definition: at least one of (website, country, founded_year) is None.
    # We don't include is_independent in the missing check because it defaults
    # to True so it's never NULL — but we still want to enrich it for auto-
    # discovered makers since the default might be wrong.
    yield from (
        session.query(Maker)
        .filter(
            (Maker.website.is_(None))
            | (Maker.country.is_(None))
            | (Maker.founded_year.is_(None))
            | (Maker.enriched_at.is_(None))
        )
        .filter(Maker.is_suppressed.isnot(True))
        .filter(Maker.reviewed_at.is_(None))
        .order_by(Maker.canonical_name)
        .all()
    )


def _current_value(maker: Maker, field_name: str) -> Any:
    """Read the current value of a field on a Maker."""
    return getattr(maker, field_name)


def _propose_for_maker(
    session: Session, maker: Maker, only_auto: bool = False
) -> EnrichmentProposal:
    """Compute proposals for all enrichable fields on one maker.

    Skips fields that already have a non-None value (never-overwrite rule).
    `only_auto`: if True, only consider fields with auto_enrich=True.
    """
    proposal = EnrichmentProposal(
        maker_id=maker.id,
        canonical_name=maker.canonical_name,
        country=maker.country,
    )
    for field in FIELDS:
        if only_auto and not field.auto_enrich:
            continue
        current = _current_value(maker, field.name)
        # Special case for is_independent: the default is True, so we can't
        # use "is None" as the gate. Instead, the gate is whether the field
        # has been explicitly enriched before. If enriched_fields includes
        # this field, skip it.
        if field.name == "is_independent":
            if maker.enriched_fields and field.name in maker.enriched_fields:
                continue
            # If is_independent has been set to non-default via seed YAML,
            # that's signaled by enriched_fields being None and the value
            # being False (since default is True). We respect explicit False.
            # For the True case, we may overwrite with a confident True
            # result from the LLM (no semantic change) just to mark it
            # enriched.
        else:
            if current is not None:
                continue

        value = field.lookup_fn(session, maker)
        verified, reason = field.verify_fn(value)
        proposal.field_proposals.append(
            FieldProposal(
                field_name=field.name,
                proposed_value=value,
                verified=verified,
                rejection_reason=reason,
            )
        )
    return proposal


def propose_enrichments(
    session: Session, only_auto: bool = False
) -> list[EnrichmentProposal]:
    """Compute proposed enrichments for every unenriched Maker.

    Calls the LLM (cached) for each missing field. Does NOT write to
    the database. Caller decides whether to commit.

    `only_auto`: when True, restrict to fields marked auto_enrich=True.
    Used by automatic on-scan enrichment.
    """
    proposals: list[EnrichmentProposal] = []
    for maker in iter_unenriched_makers(session, only_auto_enrichable=only_auto):
        proposals.append(_propose_for_maker(session, maker, only_auto=only_auto))
    return proposals


def apply_enrichments(
    session: Session, proposals: list[EnrichmentProposal]
) -> int:
    """Write committable proposals to the database.

    For each EnrichmentProposal, writes each FieldProposal that's
    committable. Updates enriched_at and enriched_fields per maker.

    Returns the count of fields written (not makers — a maker that gets
    both website and country contributes 2 to the count).

    Defensive: re-checks current values before writing in case the row
    was modified between propose and apply. The row is re-read from the
    database (populate_existing) rather than trusted from the session —
    sessions don't expire on commit, so a CLI run would otherwise check
    values loaded before its lookups started — and a maker reviewed on
    /review in the meantime is skipped (4N/4O).
    """
    count = 0
    for prop in proposals:
        committable = prop.committable_proposals
        if not committable:
            continue
        maker = (
            session.query(Maker)
            .populate_existing()
            .filter_by(id=prop.maker_id)
            .first()
        )
        if maker is None:
            logger.warning("Maker id=%d disappeared between propose and apply", prop.maker_id)
            continue
        if maker.reviewed_at is not None:
            logger.info(
                "Skipping %s: reviewed between propose and apply", maker.canonical_name
            )
            continue
        wrote_any = False
        for fp in committable:
            field = _field_by_name(fp.field_name)
            if field is None:
                continue
            # Defensive re-check (never-overwrite invariant)
            if fp.field_name == "is_independent":
                # is_independent has a default, so we re-check via
                # enriched_fields rather than None.
                if maker.enriched_fields and fp.field_name in maker.enriched_fields:
                    logger.info(
                        "Skipping %s.%s: was enriched between propose and apply",
                        maker.canonical_name, fp.field_name,
                    )
                    continue
            else:
                if _current_value(maker, fp.field_name) is not None:
                    logger.info(
                        "Skipping %s.%s: was set between propose and apply",
                        maker.canonical_name, fp.field_name,
                    )
                    continue
            setattr(maker, fp.field_name, fp.proposed_value)
            existing = list(maker.enriched_fields or [])
            if fp.field_name not in existing:
                existing.append(fp.field_name)
            maker.enriched_fields = existing
            wrote_any = True
            count += 1
        if wrote_any:
            maker.enriched_at = datetime.now(timezone.utc)
    session.commit()
    return count


def auto_enrich_unenriched_makers(session: Session, limit: int = 25) -> int:
    """Run automatic enrichment on Makers with enriched_at IS NULL.

    Called by the orchestrator after a scan completes (2T).
    Commits immediately (no dry-run path). Capped at `limit` makers per
    invocation to prevent runaway costs from a buggy scan that
    accidentally creates many makers.

    Returns the count of fields written.

    Errors during a single maker's enrichment are caught and logged;
    other makers in the batch continue. Catastrophic errors (e.g., API
    key missing) propagate so the scan log shows them clearly.
    """
    unenriched = list(iter_unenriched_makers(session, only_auto_enrichable=True))
    if not unenriched:
        return 0

    # Apply the cap before doing any LLM work.
    if len(unenriched) > limit:
        logger.info(
            "Auto-enrichment: %d unenriched makers, processing first %d "
            "(remainder will be picked up on next scan)",
            len(unenriched), limit,
        )
        unenriched = unenriched[:limit]
    else:
        logger.info("Auto-enrichment: processing %d unenriched makers", len(unenriched))

    proposals: list[EnrichmentProposal] = []
    for maker in unenriched:
        try:
            proposals.append(_propose_for_maker(session, maker, only_auto=True))
        except Exception as exc:
            logger.warning(
                "Auto-enrichment failed for %s: %s — skipping this maker",
                maker.canonical_name, exc,
            )

    return apply_enrichments(session, proposals)


# ---------- Taste tagging for auto-discovered makers (chunk 3F) ----------
#
# The factual fields above (website/country/is_independent) are filled on the
# `enriched_at IS NULL` path. Taste is handled separately because:
#
#   1. Its gate is `taste_enriched_at IS NULL`, not `enriched_at IS NULL` — so
#      makers already enriched for the factual fields (enriched_at set) still
#      get their taste tagged on a later scan.
#   2. The LLM abstains heavily by design, so we stamp `taste_enriched_at` on
#      every attempt (even a full abstention) rather than retrying forever.
#   3. Only auto-discovered independents are in scope; seed makers get their
#      taste from the curated YAML via the seed loader, and conglomerates are
#      out of the discovery vocabulary entirely.
#
# Values are sanitized against the controlled vocabulary (taste.sanitize_taste)
# and written with the same "auto" marker (enriched_fields) the factual fields
# use, so the discoveries view / profile panel flag them for human review.


@dataclass
class TasteProposal:
    """Sanitized, in-vocabulary taste tags drafted for one maker.

    ``tags`` may be empty if the model abstained on everything — such a
    proposal still stamps ``taste_enriched_at`` on apply so the maker isn't
    retried, but writes no fields.
    """

    maker_id: int
    canonical_name: str
    tags: dict


def _has_any_taste(maker: Maker) -> bool:
    """True if the maker already carries at least one taste field value."""
    return any(_current_value(maker, f) for f in taste.TASTE_FIELDS)


def iter_untagged_discovered_independents(
    session: Session, include_attempted: bool = False
) -> list[Maker]:
    """Auto-discovered independents that need taste tags.

    "Auto-discovered" = canonical_name not in the seed YAML (the same
    Definition A the discoveries view uses). Ordered by canonical_name for
    stable dry-run output. Suppressed makers are excluded — a hidden maker
    shouldn't cost taste-tagging LLM calls.

    `include_attempted` toggles the gate:

    - False (default, and the on-scan path): only makers with
      `taste_enriched_at IS NULL` — never attempted. This is what keeps the
      heavy-abstaining tagger from re-querying the same makers every scan.
    - True (the `--retag` path): any maker that currently has *no* taste at
      all, regardless of whether it was attempted before. This re-opens the
      previously-abstained makers (e.g. for a better, context-aware prompt)
      while still skipping anything that already got tags.

    Reviewed makers (saved on /review, chunk 4N) are excluded under both
    gates: their taste was set by hand, and taste deliberately left blank
    would otherwise be redrafted by `--retag`.
    """
    from watch_app.scoring.discovery import _load_seed_names

    seed_names = _load_seed_names()
    query = session.query(Maker).filter(
        Maker.is_independent.is_(True),
        Maker.is_suppressed.isnot(True),
        Maker.reviewed_at.is_(None),
    )
    if not include_attempted:
        query = query.filter(Maker.taste_enriched_at.is_(None))
    candidates = query.order_by(Maker.canonical_name).all()

    result: list[Maker] = []
    for m in candidates:
        if m.canonical_name in seed_names:
            continue
        if include_attempted and _has_any_taste(m):
            continue  # already tagged — --retag only re-attempts the untagged
        result.append(m)
    return result


def _gather_taste_context(
    maker: Maker, max_mentions: int = 5, max_chars: int = 1200
) -> str | None:
    """Build a compact press-mention context block for the taste prompt.

    Pulls the maker's mention titles + excerpts (most recent first,
    de-duplicated by title), which for niche makers carries far more signal
    than the bare name. Returns None when there's nothing useful to send.
    """
    mentions = sorted(
        maker.mentions,
        key=lambda x: (x.published_at or x.discovered_at),
        reverse=True,
    )
    lines: list[str] = []
    seen_titles: set[str] = set()
    for m in mentions:
        title = (m.title or "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        excerpt = (m.excerpt or "").strip().replace("\n", " ")
        line = f"- {title}" + (f": {excerpt[:200]}" if excerpt else "")
        lines.append(line)
        if len(lines) >= max_mentions:
            break
    if not lines:
        return None
    block = "\n".join(lines)
    return block[:max_chars]


def _propose_taste_for_maker(session: Session, maker: Maker) -> TasteProposal:
    """Look up + sanitize taste tags for one maker. No DB writes."""
    context = _gather_taste_context(maker)
    raw = lookup_maker_taste(
        session, maker.canonical_name, country=maker.country, context=context
    )
    clean = taste.sanitize_taste(raw)
    return TasteProposal(
        maker_id=maker.id, canonical_name=maker.canonical_name, tags=clean
    )


def propose_taste_for_discovered(
    session: Session, limit: int | None = None, include_attempted: bool = False
) -> list[TasteProposal]:
    """Draft taste tags for untagged discovered independents. No DB writes.

    Calls the (cached) LLM once per maker. `limit` caps the number of makers
    considered; None means all. `include_attempted` re-opens previously
    abstained makers (the `--retag` path). Used by the `cli enrich --taste`
    dry-run.
    """
    makers = iter_untagged_discovered_independents(
        session, include_attempted=include_attempted
    )
    if limit is not None and len(makers) > limit:
        makers = makers[:limit]
    return [_propose_taste_for_maker(session, m) for m in makers]


def apply_taste_proposals(
    session: Session, proposals: list[TasteProposal]
) -> int:
    """Write drafted taste tags to the DB and stamp taste_enriched_at.

    Returns the count of taste field values written. Every maker in
    `proposals` is stamped with taste_enriched_at (even on abstention) so it
    isn't re-attempted. The never-overwrite rule applies: a taste field that
    already has a value is left untouched. Written fields are added to
    enriched_fields so the UI marks them "auto". Each row is re-read before
    writing, and a maker reviewed since propose is skipped (see
    apply_enrichments).
    """
    count = 0
    for prop in proposals:
        maker = (
            session.query(Maker)
            .populate_existing()
            .filter_by(id=prop.maker_id)
            .first()
        )
        if maker is None:
            logger.warning(
                "Maker id=%d disappeared between propose and apply", prop.maker_id
            )
            continue
        if maker.reviewed_at is not None:
            logger.info(
                "Skipping %s: reviewed between propose and apply", maker.canonical_name
            )
            continue
        existing_fields = list(maker.enriched_fields or [])
        for field_name, value in prop.tags.items():
            if _current_value(maker, field_name) is not None:
                continue
            setattr(maker, field_name, value)
            if field_name not in existing_fields:
                existing_fields.append(field_name)
            count += 1
        maker.enriched_fields = existing_fields
        # Stamp regardless of whether anything was written, so an abstention
        # isn't retried on every future scan.
        maker.taste_enriched_at = datetime.now(timezone.utc)
    session.commit()
    return count


def auto_tag_taste_for_discovered(session: Session, limit: int = 25) -> int:
    """Automatically taste-tag auto-discovered independents on scan (3F).

    Called by the orchestrator after the factual enrichment phase. Commits
    immediately; capped at `limit` makers per scan to bound LLM cost. A single
    maker's failure is logged and skipped so the batch continues.

    Returns the count of taste field values written.
    """
    makers = iter_untagged_discovered_independents(session)
    if not makers:
        return 0

    if len(makers) > limit:
        logger.info(
            "Taste tagging: %d untagged discovered makers, processing first %d "
            "(remainder picked up on next scan)",
            len(makers), limit,
        )
        makers = makers[:limit]
    else:
        logger.info("Taste tagging: processing %d untagged discovered makers", len(makers))

    proposals: list[TasteProposal] = []
    for maker in makers:
        try:
            proposals.append(_propose_taste_for_maker(session, maker))
        except Exception as exc:
            logger.warning(
                "Taste tagging failed for %s: %s — skipping this maker",
                maker.canonical_name, exc,
            )

    return apply_taste_proposals(session, proposals)


# ---------- Web-researched audit (chunk 4O) ----------
#
# One research pass per discovered maker waiting in the /review queue: the LLM
# looks the brand up with web search (lookup_maker_audit) and drafts a scope
# verdict, the brand a person / partial / model name belongs to, a pre-launch
# flag, and every field on the card, citing the pages it relied on. Every
# drafted value is checked here before anything is written: the website must
# resolve and not be a dealer / social / press site, the country must be on the
# known list, the founded year in range, taste in vocabulary.
#
# Write policy. The audit fills blank fields and replaces values still marked
# "auto" — an earlier guess-only draft — and marks whatever it writes "auto",
# so the queue shows it for checking. It never replaces a value without the
# marker (one you entered, or one that predates the marker — treated the same)
# and never touches a reviewed maker. is_independent has no blank state, so its
# default True counts as unset; only a False without the marker is yours. A
# "no" verdict writes no fields: that card is headed for suppress, delete,
# merge or rename. Applying also stamps enriched_at / taste_enriched_at, so the
# older guess-only passes leave an audited maker alone.
#
# CLI-only (`cli enrich --audit`), gated once per maker by audited_at.

VERDICTS: tuple[str, ...] = AUDIT_VERDICTS
MIN_FOUNDED_YEAR = 1700

#: Consecutive failed lookups after which a run stops — the cause is then
#: systemic (no API key, web search disabled, network down), not one maker.
MAX_CONSECUTIVE_AUDIT_FAILURES = 3


@dataclass
class AuditProposal:
    """One maker's research, verified and ready to apply.

    `fields` holds only drafted values that passed their checks; `rejected`
    maps a field to why its drafted value was dropped (the dry run shows
    these). `error` is set when the lookup produced nothing — such a maker
    isn't stamped, so the next run retries it.
    """

    maker_id: int
    canonical_name: str
    verdict: str | None = None
    reason: str | None = None
    brand_name: str | None = None
    pre_launch: bool | None = None
    fields: dict = dataclass_field(default_factory=dict)
    rejected: dict = dataclass_field(default_factory=dict)
    sources: list = dataclass_field(default_factory=list)
    usage: dict = dataclass_field(default_factory=dict)
    cached: bool = False
    error: str | None = None


def iter_unaudited_makers(session: Session) -> list[Maker]:
    """Discovered makers waiting for the audit, in /review queue order.

    Non-seed, not suppressed, not reviewed, never audited. Ranked makers come
    first in rank order under the default preset, then the rest newest-first —
    so `--limit N` researches the top of the review queue first.
    """
    from watch_app.config import get_settings
    from watch_app.scoring.discovery import _load_seed_names
    from watch_app.scoring.queries import fetch_top_makers

    seed_names = _load_seed_names()
    rank_by_id = {
        r["maker_id"]: r["rank"]
        for r in fetch_top_makers(
            session, get_settings().default_preset, limit=100_000
        )
    }
    makers = [
        m
        for m in session.query(Maker)
        .filter(
            Maker.is_suppressed.isnot(True),
            Maker.reviewed_at.is_(None),
            Maker.audited_at.is_(None),
        )
        .order_by(Maker.first_seen_at.desc())
        .all()
        if m.canonical_name not in seed_names
    ]
    # Stable sort: the unranked tail keeps the query's newest-first order.
    makers.sort(
        key=lambda m: (rank_by_id.get(m.id) is None, rank_by_id.get(m.id) or 0)
    )
    return makers


def _verify_audit_fields(raw: dict) -> tuple[dict, dict]:
    """Check each drafted value. Returns (accepted, rejected) where rejected
    maps a field to the reason its value was dropped. Taste values outside the
    vocabulary are reported even when the rest of the list survives — a
    recurring one is a vocabulary gap worth knowing about."""
    accepted: dict = {}
    rejected: dict = {}

    country = raw.get("country")
    if country is not None:
        if isinstance(country, str) and country in KNOWN_WATCHMAKING_COUNTRIES:
            accepted["country"] = country
        else:
            rejected["country"] = f"not a known country: {country!r}"

    year = raw.get("founded_year")
    if year is not None:
        try:
            value = int(year) if not isinstance(year, bool) else None
        except (TypeError, ValueError):
            value = None
        this_year = datetime.now(timezone.utc).year
        if value is not None and MIN_FOUNDED_YEAR <= value <= this_year:
            accepted["founded_year"] = value
        else:
            rejected["founded_year"] = f"not a year in {MIN_FOUNDED_YEAR}–{this_year}: {year!r}"

    website = raw.get("website")
    if website is not None:
        if isinstance(website, str):
            ok, reason = _verify_website(website.strip())
            if ok:
                accepted["website"] = website.strip()
            else:
                rejected["website"] = reason or "rejected"
        else:
            rejected["website"] = f"not a URL: {website!r}"

    independent = raw.get("is_independent")
    if independent is not None:
        if isinstance(independent, bool):
            accepted["is_independent"] = independent
        else:
            rejected["is_independent"] = f"not true/false: {independent!r}"

    drafted_taste = {f: raw[f] for f in taste.TASTE_FIELDS if raw.get(f) is not None}
    clean = taste.sanitize_taste(drafted_taste)
    accepted.update(clean)
    for field_name, value in drafted_taste.items():
        if field_name not in clean:
            rejected[field_name] = f"out of vocabulary: {value!r}"
        elif field_name in taste.MULTI_VALUE_FIELDS:
            dropped = [v for v in value if v not in clean[field_name]]
            if dropped:
                rejected[field_name] = f"dropped out-of-vocabulary {dropped!r}"
    return accepted, rejected


def _build_audit_proposal(maker: Maker, result: dict) -> AuditProposal:
    """Turn a lookup result into a verified proposal. A "no" keeps its fields
    empty; a brand_name that's just this maker's own name is dropped."""
    verdict = result.get("verdict") if result.get("verdict") in VERDICTS else None
    reason = result.get("reason")
    brand = result.get("brand_name")
    if isinstance(brand, str) and _normalize(brand) == _normalize(maker.canonical_name):
        brand = None
    pre_launch = result.get("pre_launch")
    proposal = AuditProposal(
        maker_id=maker.id,
        canonical_name=maker.canonical_name,
        verdict=verdict,
        reason=reason.strip()[:300] if isinstance(reason, str) and reason.strip() else None,
        brand_name=brand.strip()[:200] if isinstance(brand, str) and brand.strip() else None,
        pre_launch=pre_launch if isinstance(pre_launch, bool) else None,
        sources=list(result.get("sources") or []),
        usage=dict(result.get("usage") or {}),
        cached=bool(result.get("cached")),
    )
    if verdict != "no":
        proposal.fields, proposal.rejected = _verify_audit_fields(
            result.get("fields") or {}
        )
    return proposal


def propose_audits(
    session: Session,
    limit: int | None = None,
    progress: Callable[[int, int, AuditProposal], None] | None = None,
) -> list[AuditProposal]:
    """Research unaudited discovered makers, in review-queue order. No maker
    writes — but each lookup's cache entry is committed the moment it returns,
    so a dry run followed by --commit, or an interrupted run, never pays twice.

    A maker whose lookup fails or produces nothing comes back with `error`
    set; it isn't stamped, so the next run retries it. After
    MAX_CONSECUTIVE_AUDIT_FAILURES failures in a row the run stops with a
    RuntimeError — the cause is systemic, and what's been paid for is cached.
    `progress(i, total, proposal)` is called after each maker (a full run is
    long enough to want a heartbeat).
    """
    makers = iter_unaudited_makers(session)
    if limit is not None:
        makers = makers[:limit]
    proposals: list[AuditProposal] = []

    def add(proposal: AuditProposal) -> None:
        proposals.append(proposal)
        if progress is not None:
            progress(len(proposals), len(makers), proposal)

    consecutive_failures = 0
    for maker in makers:
        maker_id, name = maker.id, maker.canonical_name
        try:
            result = lookup_maker_audit(
                session, name, country=maker.country, context=_gather_taste_context(maker)
            )
        except Exception as exc:
            session.rollback()
            logger.warning("Audit of %s failed: %s", name, exc)
            result, error = None, f"lookup failed: {exc}"
        else:
            session.commit()  # keep what's been paid for, whatever happens next
            error = None if result is not None else "no usable answer"
        if error:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_AUDIT_FAILURES:
                raise RuntimeError(
                    f"Stopped after {consecutive_failures} failed lookups in a row; "
                    f"last: {name} — {error}"
                )
            add(AuditProposal(maker_id, name, error=error))
            continue
        consecutive_failures = 0
        add(_build_audit_proposal(maker, result))
    return proposals


def _is_human_value(maker: Maker, field_name: str, auto_fields: list[str]) -> bool:
    """True if a field holds a value the audit must not replace: a non-blank
    value without the "auto" marker. is_independent's default True doesn't
    count — only a False without the marker is a deliberate call."""
    if field_name in auto_fields:
        return False
    value = getattr(maker, field_name)
    if field_name == "is_independent":
        return value is False
    return value not in (None, [], "")


def apply_audits(session: Session, proposals: list[AuditProposal]) -> int:
    """Write verified audit results; returns the number of field values changed.

    Each row is re-read first (a full run can take most of an hour) and a maker
    reviewed since its research ran is skipped. Proposals with `error` write
    nothing and aren't stamped. See the write policy above.
    """
    count = 0
    now = datetime.now(timezone.utc)
    for prop in proposals:
        if prop.error:
            continue
        maker = (
            session.query(Maker)
            .populate_existing()
            .filter_by(id=prop.maker_id)
            .first()
        )
        if maker is None:
            logger.warning("Maker id=%d disappeared before its audit was applied", prop.maker_id)
            continue
        if maker.reviewed_at is not None:
            logger.info("Skipping audit of %s: reviewed since it ran", maker.canonical_name)
            continue

        auto = list(maker.enriched_fields or [])
        drafts = dict(prop.fields)
        if prop.verdict != "no" and prop.pre_launch is True:
            drafts["is_pre_launch"] = True
        for field_name, value in drafts.items():
            if _is_human_value(maker, field_name, auto):
                continue
            if getattr(maker, field_name) != value:
                setattr(maker, field_name, value)
                count += 1
            if field_name not in auto:
                auto.append(field_name)
        maker.enriched_fields = auto

        maker.audit_verdict = prop.verdict
        maker.audit_reason = prop.reason
        maker.audit_brand = prop.brand_name
        maker.audit_sources = prop.sources or None
        maker.audited_at = now
        maker.enriched_at = now
        maker.taste_enriched_at = now
    session.commit()
    return count
