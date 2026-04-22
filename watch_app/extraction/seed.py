"""Load the curated known-makers seed list into the Makers table.

Idempotent: safe to run on every scan. New makers get created; existing
makers have their metadata (country, founded_year, aliases, is_independent)
updated from the YAML.

Rationale for running every scan: the YAML is the source of truth for
curated metadata. If we update the YAML (add a new maker, fix a country),
the next scan picks it up. We never delete makers via the seed path — a
maker removed from YAML stays in the DB with whatever mentions they've
accumulated.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from watch_app.storage.models import Maker


logger = logging.getLogger(__name__)


SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "known_makers.yaml"


def _load_yaml() -> list[dict]:
    """Return the raw list of maker dicts from the YAML file."""
    with SEED_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "makers" not in data:
        return []
    return data["makers"]


def _merge_aliases(existing: list[str] | None, incoming: list[str] | None) -> list[str]:
    """Union of alias lists, preserving order and dropping duplicates."""
    seen: set[str] = set()
    result: list[str] = []
    for lst in (existing or [], incoming or []):
        for alias in lst:
            if alias not in seen:
                seen.add(alias)
                result.append(alias)
    return result


def load_seed_makers(session: Session) -> dict[str, int]:
    """Upsert every maker in the seed YAML. Returns stats dict."""
    raw = _load_yaml()
    stats = {"created": 0, "updated": 0, "skipped": 0}

    for entry in raw:
        canonical = (entry.get("canonical_name") or "").strip()
        if not canonical:
            stats["skipped"] += 1
            continue

        existing = (
            session.query(Maker)
            .filter(Maker.canonical_name == canonical)
            .one_or_none()
        )

        aliases = entry.get("aliases") or []
        country = entry.get("country")
        founded_year = entry.get("founded_year")
        is_independent = entry.get("is_independent", True)

        if existing is None:
            session.add(
                Maker(
                    canonical_name=canonical,
                    aliases=aliases,
                    country=country,
                    founded_year=founded_year,
                    is_independent=is_independent,
                )
            )
            stats["created"] += 1
        else:
            existing.aliases = _merge_aliases(existing.aliases, aliases)
            if country and not existing.country:
                existing.country = country
            if founded_year and not existing.founded_year:
                existing.founded_year = founded_year
            # Overwrite is_independent from YAML — it's the authoritative source
            # for curated entries.
            existing.is_independent = is_independent
            stats["updated"] += 1

    logger.info(
        "Seed makers: created=%d updated=%d skipped=%d",
        stats["created"],
        stats["updated"],
        stats["skipped"],
    )
    return stats
