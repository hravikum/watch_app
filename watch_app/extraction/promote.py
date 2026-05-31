"""Promote an auto-discovered maker into the curated seed YAML (chunk 3L).

A discovered maker lives only in the DB. Promoting it serializes its current
curated fields into a new ``known_makers.yaml`` entry and appends it under a
trailing "Promoted from discovery" section; the operator then reviews the
``git diff`` and commits. On the next scan the seed loader reconciles onto the
existing DB row (matched by canonical_name, so id/mentions/scores survive), and
because ``_load_seed_names()`` reads the YAML live, the maker leaves the
"discovered" universe immediately.

The serialized entry captures exactly the maker's current taste, so the
loader's overwrite of taste / is_independent is a no-op rather than a clobber.
Null dimensions are omitted (the loader leaves omitted fields untouched).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from watch_app import taste
from watch_app.extraction.resolver import _normalize
from watch_app.extraction.seed import SEED_PATH, _load_yaml
from watch_app.storage.models import Maker

# Score (0-100, WRatio) at/above which a non-exact name is flagged as a likely
# duplicate the operator should confirm. High on purpose — this only catches
# near-identical names, not merely related ones.
NEAR_MATCH_THRESHOLD = 88

PROMOTED_SECTION = "  # --- Promoted from discovery ---"

# Field order for a serialized entry, matching the hand-curated ones.
_ENTRY_ORDER = [
    "aesthetic_tags",
    "maker_type",
    "price_band",
    "output_scale",
    "distribution",
    "complication_focus",
    "movement_origin",
    "website",
    "aliases",
    "country",
    "founded_year",
]


@dataclass
class PromoteResult:
    canonical_name: str
    entry_text: str
    written: bool = False
    blockers: list[str] = field(default_factory=list)       # hard — never write
    needs_confirm: list[str] = field(default_factory=list)  # write only with force
    warnings: list[str] = field(default_factory=list)       # informational


def _has_taste(m: Maker) -> bool:
    return any(getattr(m, f) for f in taste.TASTE_FIELDS)


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def build_entry_lines(maker: Maker) -> list[str]:
    """Serialize a maker's curated fields as YAML list-entry lines.

    Mirrors the hand-written entries: double-quoted scalars, flow-style lists,
    founded_year bare, is_independent written only when false (true is the
    schema default), and null/empty fields omitted.
    """
    lines = [f'  - canonical_name: "{_esc(maker.canonical_name)}"\n']
    values = {
        "aesthetic_tags": maker.aesthetic_tags,
        "maker_type": maker.maker_type,
        "price_band": maker.price_band,
        "output_scale": maker.output_scale,
        "distribution": maker.distribution,
        "complication_focus": maker.complication_focus,
        "movement_origin": maker.movement_origin,
        "website": maker.website,
        "aliases": maker.aliases,
        "country": maker.country,
        "founded_year": maker.founded_year,
    }
    for name in _ENTRY_ORDER:
        v = values.get(name)
        if v is None or v == [] or v == "":
            continue
        if name == "founded_year":
            lines.append(f"    {name}: {int(v)}\n")
        elif isinstance(v, list):
            inner = ", ".join(f'"{_esc(x)}"' for x in v)
            lines.append(f"    {name}: [{inner}]\n")
        else:
            lines.append(f'    {name}: "{_esc(v)}"\n')
    if maker.is_independent is False:
        lines.append("    is_independent: false\n")
    return lines


def check_promotable(maker: Maker) -> PromoteResult:
    """Build the entry text and run collision + advisory checks (no writes)."""
    result = PromoteResult(
        canonical_name=maker.canonical_name,
        entry_text="".join(build_entry_lines(maker)),
    )

    # Normalized lookup of existing seed canonical names + aliases.
    seed_norm: dict[str, str] = {}
    for entry in _load_yaml():
        cn = entry.get("canonical_name")
        if cn:
            seed_norm.setdefault(_normalize(cn), cn)
        for alias in entry.get("aliases") or []:
            seed_norm.setdefault(_normalize(alias), cn or alias)

    candidate_names = [maker.canonical_name, *(maker.aliases or [])]
    candidate_norms = {_normalize(n) for n in candidate_names}

    # Hard block: exact (normalized) collision with any existing seed name/alias.
    for name in candidate_names:
        norm = _normalize(name)
        if norm in seed_norm:
            result.blockers.append(
                f"'{name}' already matches seed entry '{seed_norm[norm]}'."
            )

    # Confirm-required: a high fuzzy match that isn't an exact hit.
    if seed_norm and not result.blockers:
        best = process.extractOne(
            _normalize(maker.canonical_name),
            list(seed_norm.keys()),
            scorer=fuzz.WRatio,
        )
        if best is not None:
            key, score, _ = best
            if score >= NEAR_MATCH_THRESHOLD and key not in candidate_norms:
                result.needs_confirm.append(
                    f"'{maker.canonical_name}' looks similar to seed entry "
                    f"'{seed_norm[key]}' (score {int(score)}) — confirm it's distinct."
                )

    # Soft warning: promoting without taste tags.
    if not _has_taste(maker):
        result.warnings.append(
            f"'{maker.canonical_name}' has no taste tags yet — usually you'd tag "
            f"it in /review before promoting."
        )
    return result


def append_to_yaml(entry_text: str, path: Path | None = None) -> None:
    """Append an entry under the trailing 'Promoted from discovery' section.

    Builds the new file content in memory and validates it (parses cleanly, the
    new entry round-trips, its taste is in vocabulary) BEFORE writing — so a
    malformed entry never lands a half-written file on disk.
    """
    path = path or SEED_PATH
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    if PROMOTED_SECTION.strip() not in text:
        text += "\n" + PROMOTED_SECTION + "\n"
    text += entry_text

    data = yaml.safe_load(text)  # raises yaml.YAMLError on malformed output
    makers = (data or {}).get("makers") or []
    if not makers:
        raise ValueError("Refusing to write: parsed YAML has no makers.")
    # Validate every entry's taste so a promotion can't slip a bad value in.
    for entry in makers:
        taste.validate_taste_fields(entry)

    path.write_text(text, encoding="utf-8")


def promote_to_seed(
    session: Session,
    maker: Maker,
    *,
    commit: bool = False,
    force: bool = False,
    path: Path | None = None,
) -> PromoteResult:
    """Promote a discovered maker into the seed YAML.

    Dry-run by default (returns the entry + checks, writes nothing). With
    ``commit`` it writes unless there's a blocker, or a needs-confirm without
    ``force``.
    """
    result = check_promotable(maker)
    if not commit:
        return result
    if result.blockers:
        return result
    if result.needs_confirm and not force:
        return result
    append_to_yaml(result.entry_text, path=path)
    result.written = True
    return result
