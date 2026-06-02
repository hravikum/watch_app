"""Discovery queries — auto-discovered makers not in the curated seed.

The resolver creates Maker rows for any brand name it finds in an article
that doesn't already exist. Those rows are how the tool surfaces makers
the seed YAML doesn't know about — but until this module, there was no
view that distinguished "auto-discovered" from "seed-curated."

Definition A: a maker is auto-discovered if its canonical_name does NOT
appear in the seed YAML's canonical_name or aliases. We match against
the YAML at query time rather than denormalizing the status onto Maker
rows — the seed YAML is the source of truth, and computing match status
on demand keeps things consistent if the YAML changes.

Performance: the seed loader loads 125 entries with ~50 aliases total.
Building the matched-name set is fast (~ms), and we cache it per request
within the query function.

Returns DiscoveredMaker dataclasses, not raw rows, to keep the template
layer clean (template doesn't need to know about SQLAlchemy objects).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from watch_app.storage.models import Maker, Mention, Source


SEED_YAML_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "known_makers.yaml"
)


@dataclass
class DiscoveredMaker:
    """One row in the discovery view.

    Includes everything the template needs to render a useful row:
    name, when first seen, what we know, and what's still missing.
    """

    id: int
    canonical_name: str
    country: str | None
    founded_year: int | None
    website: str | None
    is_independent: bool
    first_seen_at: datetime
    enriched_at: datetime | None
    enriched_fields: list[str]
    mention_count: int
    source_names: list[str]
    relationship_state: str | None = None

    @property
    def missing_fields(self) -> list[str]:
        """Which key fields are still None on this maker."""
        out = []
        if self.country is None:
            out.append("country")
        if self.founded_year is None:
            out.append("founded_year")
        if self.website is None:
            out.append("website")
        return out

    def field_was_enriched(self, field_name: str) -> bool:
        """Was a specific field populated by LLM enrichment (vs seed/manual)?"""
        return field_name in (self.enriched_fields or [])


def _load_seed_names() -> set[str]:
    """Read the seed YAML and return a set of all canonical names + aliases.

    Called once per discovery query. The YAML is small (~125 entries) so
    re-reading it on each request has negligible cost — and it ensures we
    always reflect the current YAML state.
    """
    if not SEED_YAML_PATH.exists():
        return set()
    data = yaml.safe_load(SEED_YAML_PATH.read_text()) or {}
    makers = data.get("makers") or []
    names: set[str] = set()
    for entry in makers:
        if not isinstance(entry, dict):
            continue
        canonical = entry.get("canonical_name")
        if canonical:
            names.add(canonical)
        for alias in entry.get("aliases") or []:
            if isinstance(alias, str):
                names.add(alias)
    return names


def fetch_discovered_makers(
    session: Session, since: datetime | None = None
) -> list[DiscoveredMaker]:
    """Return all auto-discovered makers, sorted by first_seen_at descending.

    Auto-discovered = canonical_name not present in the seed YAML's
    names or aliases (Definition A).

    When `since` is given, restrict to makers first seen on or after that
    instant (used by the weekly digest's "new discoveries this window"
    section). The comparison runs in SQL — SQLite returns naive datetimes,
    so a Python-side compare against a tz-aware cutoff would raise.

    Includes mention count and the list of sources that mentioned each
    maker, so the discovery view can show context without N+1 queries
    from the template.
    """
    seed_names = _load_seed_names()

    # Find Makers whose canonical_name isn't in the seed.
    # Using a Python set filter rather than a SQL NOT IN with a large list:
    # for ~125 names this is comparable in performance and far more readable.
    query = session.query(Maker).filter(Maker.is_suppressed.is_(False))
    if since is not None:
        query = query.filter(Maker.first_seen_at >= since)
    makers = query.order_by(Maker.first_seen_at.desc()).all()
    auto_discovered = [m for m in makers if m.canonical_name not in seed_names]

    if not auto_discovered:
        return []

    # Batch-fetch mention counts and source names per maker. Single query
    # each, then group in Python — avoids N+1.
    maker_ids = [m.id for m in auto_discovered]

    mention_counts: dict[int, int] = dict(
        session.execute(
            select(Mention.maker_id, func.count(Mention.id))
            .where(Mention.maker_id.in_(maker_ids))
            .group_by(Mention.maker_id)
        ).all()
    )

    # Source names per maker via a single join query.
    source_rows = session.execute(
        select(Mention.maker_id, Source.name)
        .join(Source, Source.id == Mention.source_id)
        .where(Mention.maker_id.in_(maker_ids))
        .distinct()
    ).all()
    sources_by_maker: dict[int, list[str]] = {}
    for maker_id, source_name in source_rows:
        sources_by_maker.setdefault(maker_id, []).append(source_name)
    # Sort source names per maker for stable display.
    for ids in sources_by_maker.values():
        ids.sort()

    return [
        DiscoveredMaker(
            id=m.id,
            canonical_name=m.canonical_name,
            country=m.country,
            founded_year=m.founded_year,
            website=m.website,
            is_independent=m.is_independent,
            first_seen_at=m.first_seen_at,
            enriched_at=m.enriched_at,
            enriched_fields=m.enriched_fields or [],
            mention_count=mention_counts.get(m.id, 0),
            source_names=sources_by_maker.get(m.id, []),
            relationship_state=m.relationship_state,
        )
        for m in auto_discovered
    ]
