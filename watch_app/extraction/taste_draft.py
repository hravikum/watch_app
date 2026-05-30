"""Draft taste tags for untagged makers via LLM, into the seed YAML (chunk 3B).

A curation aid for Layer 1b, not a runtime path. For each untagged
INDEPENDENT maker in known_makers.yaml, ask Haiku to propose taste tags
(constrained to the controlled vocabulary), sanitize against that
vocabulary, and — with --commit — write the drafts into the YAML for human
review via ``git diff``.

Why the YAML and not the DB enrichment registry: taste is editorial. The
YAML is the curated source of truth (chunk 3A), and a human reviews every
draft. Conglomerates are skipped — the vocabulary is built for independents,
and they're in the seed only for extractor disambiguation.

The writer inserts a taste block immediately after a maker's
``canonical_name`` line, preserving all surrounding comments. It must only
be called on makers that aren't already tagged (the CLI guarantees this),
since it does not check for or replace existing taste lines.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from watch_app import taste
from watch_app.extraction.seed import SEED_PATH, _load_yaml
from watch_app.llm.client import lookup_maker_taste


logger = logging.getLogger(__name__)


# Field order used when writing a taste block into the YAML (matches the
# hand-tagged 1a entries for a consistent diff).
_WRITE_ORDER = (
    "aesthetic_tags",
    "maker_type",
    "price_band",
    "output_scale",
    "distribution",
    "complication_focus",
    "movement_origin",
)


def is_tagged(entry: dict) -> bool:
    """True if the entry already carries any taste dimension."""
    return any(entry.get(f) is not None for f in taste.TASTE_FIELDS)


def untagged_independents(entries: list[dict] | None = None) -> list[dict]:
    """Return seed entries that are independent and not yet tagged.

    These are the Layer 1b targets. Conglomerates (is_independent False) are
    excluded; so are entries with a blank canonical_name.
    """
    if entries is None:
        entries = _load_yaml()
    return [
        e
        for e in entries
        if e.get("is_independent", True)
        and not is_tagged(e)
        and (e.get("canonical_name") or "").strip()
    ]


@dataclass
class TasteProposal:
    """A sanitized, in-vocabulary taste draft for one maker.

    ``tags`` may be empty if the model abstained on everything — such
    proposals are shown in dry-run but not written.
    """

    canonical_name: str
    tags: dict


def propose_taste(session: Session, entry: dict) -> TasteProposal:
    """Draft taste tags for one seed entry via the LLM, sanitized to vocab."""
    name = entry["canonical_name"].strip()
    raw = lookup_maker_taste(session, name, country=entry.get("country"))
    clean = taste.sanitize_taste(raw)
    # Final guard — sanitize_taste should already guarantee this.
    taste.validate_taste_fields({**clean, "canonical_name": name})
    return TasteProposal(canonical_name=name, tags=clean)


def _block_lines(tags: dict) -> list[str]:
    """Render a sanitized tags dict as YAML lines, indented for a list entry."""
    lines: list[str] = []
    for field in _WRITE_ORDER:
        if field not in tags:
            continue
        value = tags[field]
        if isinstance(value, list):
            rendered = "[" + ", ".join(f'"{v}"' for v in value) + "]"
        else:
            rendered = f'"{value}"'
        lines.append(f"    {field}: {rendered}\n")
    return lines


def _name_from_line(line: str) -> str | None:
    """Extract the canonical_name value from a YAML list-entry line, or None."""
    s = line.strip()
    if not s.startswith("- canonical_name:"):
        return None
    val = s[len("- canonical_name:") :].strip()
    if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
        val = val[1:-1]
    return val


def write_drafts_to_yaml(
    proposals: list[TasteProposal], path: Path = SEED_PATH
) -> int:
    """Insert taste blocks into the YAML for proposals that have tags.

    Inserts each block immediately after the matching ``canonical_name`` line.
    Proposals with no tags are skipped. Returns the count of makers written.

    Only call on untagged makers — this does not detect or replace existing
    taste lines, so writing to an already-tagged entry would duplicate keys.
    """
    by_name = {p.canonical_name: p.tags for p in proposals if p.tags}
    if not by_name:
        return 0

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    written: set[str] = set()
    for line in lines:
        out.append(line)
        name = _name_from_line(line)
        if name in by_name and name not in written:
            out.extend(_block_lines(by_name[name]))
            written.add(name)

    path.write_text("".join(out), encoding="utf-8")
    return len(written)
