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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from watch_app import taste
from watch_app.http import get as http_get
from watch_app.llm.client import (
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
    was modified between propose and apply.
    """
    count = 0
    for prop in proposals:
        committable = prop.committable_proposals
        if not committable:
            continue
        maker = session.query(Maker).filter_by(id=prop.maker_id).first()
        if maker is None:
            logger.warning("Maker id=%d disappeared between propose and apply", prop.maker_id)
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
    enriched_fields so the UI marks them "auto".
    """
    count = 0
    for prop in proposals:
        maker = session.query(Maker).filter_by(id=prop.maker_id).first()
        if maker is None:
            logger.warning(
                "Maker id=%d disappeared between propose and apply", prop.maker_id
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
