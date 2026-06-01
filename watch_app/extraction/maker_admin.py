"""Maintenance operations on makers: merge duplicates, fix aliases, find dupes.

The remedy half of the resolver's safety net (3N). The resolver auto-aliases on
a fuzzy match; when that occasionally goes wrong, or two makers slip through as
separate rows, these let an operator clean it up. The mutating operations are
dry-run by default at the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from watch_app.extraction.resolver import _normalize
from watch_app.scoring.discovery import _load_seed_names
from watch_app.storage.models import Maker, Mention, Score, Signal, WatchModel


@dataclass
class MergePlan:
    from_name: str
    into_name: str
    mentions_reassigned: int = 0
    mentions_deduped: int = 0
    watch_models_reassigned: int = 0
    signals_deleted: int = 0
    scores_deleted: int = 0
    aliases_added: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    done: bool = False


@dataclass
class UnaliasResult:
    maker_name: str
    alias: str
    removed: bool = False
    seed_warning: bool = False
    blockers: list[str] = field(default_factory=list)


@dataclass
class DupePair:
    score: float
    a_id: int
    a_name: str
    b_id: int
    b_name: str


def _resolve_maker(session: Session, ref: str) -> Maker | None:
    """Resolve a maker by numeric id or exact canonical name."""
    maker = None
    if ref.isdigit():
        maker = session.query(Maker).filter_by(id=int(ref)).first()
    if maker is None:
        maker = session.query(Maker).filter_by(canonical_name=ref).first()
    return maker


def merge_makers(
    session: Session, from_ref: str, into_ref: str, *, commit: bool = False
) -> MergePlan:
    """Fold the ``from`` maker into ``into``: reassign its mentions and watch
    models, fold its name + aliases in as aliases, drop its derived rows, and
    delete it. Derived Signal/Score rows are deleted (recomputed next scan).

    Blocks if ``from`` is a seed maker — the loader would just re-create it; the
    YAML entry must be removed instead.
    """
    src = _resolve_maker(session, from_ref)
    dst = _resolve_maker(session, into_ref)

    blockers: list[str] = []
    if src is None:
        blockers.append(f"No maker found for '{from_ref}'.")
    if dst is None:
        blockers.append(f"No maker found for '{into_ref}'.")
    if src is not None and dst is not None and src.id == dst.id:
        blockers.append("Cannot merge a maker into itself.")
    if src is not None and src.canonical_name in _load_seed_names():
        blockers.append(
            f"'{src.canonical_name}' is a seed maker — the loader would re-create "
            f"it next scan. Remove its known_makers.yaml entry instead."
        )
    if blockers:
        return MergePlan(from_name=from_ref, into_name=into_ref, blockers=blockers)

    # Reassign mentions, de-duping on (source_id, url) against dst's existing
    # mentions (the unique key is source_id+url+maker_id, so a same-article
    # mention on both makers would otherwise collide).
    dst_keys = {
        (m.source_id, m.url)
        for m in session.query(Mention).filter_by(maker_id=dst.id)
    }
    reassigned = deduped = 0
    for men in session.query(Mention).filter_by(maker_id=src.id).all():
        if (men.source_id, men.url) in dst_keys:
            deduped += 1
            if commit:
                session.delete(men)
        else:
            reassigned += 1
            dst_keys.add((men.source_id, men.url))
            if commit:
                men.maker_id = dst.id

    watch_models = session.query(WatchModel).filter_by(maker_id=src.id).all()
    if commit:
        for wm in watch_models:
            wm.maker_id = dst.id  # gphg_url is globally unique → no collision

    signals = session.query(Signal).filter_by(maker_id=src.id).count()
    scores = session.query(Score).filter_by(maker_id=src.id).count()

    aliases_added = [
        name
        for name in [src.canonical_name, *(src.aliases or [])]
        if name and name != dst.canonical_name and name not in (dst.aliases or [])
    ]

    if commit:
        session.query(Signal).filter_by(maker_id=src.id).delete()
        session.query(Score).filter_by(maker_id=src.id).delete()
        dst.aliases = [*(dst.aliases or []), *aliases_added]
        session.delete(src)
        session.commit()

    return MergePlan(
        from_name=src.canonical_name,
        into_name=dst.canonical_name,
        mentions_reassigned=reassigned,
        mentions_deduped=deduped,
        watch_models_reassigned=len(watch_models),
        signals_deleted=signals,
        scores_deleted=scores,
        aliases_added=aliases_added,
        blockers=[],
        done=commit,
    )


def remove_alias(
    session: Session, maker_ref: str, alias: str, *, commit: bool = False
) -> UnaliasResult:
    """Remove a bad alias from a maker. Matches the alias exactly, then by
    normalized form. Warns if the maker is a seed maker, since a YAML-defined
    alias is re-added by the loader on the next scan (edit the YAML instead).
    """
    maker = _resolve_maker(session, maker_ref)
    if maker is None:
        return UnaliasResult("", alias, blockers=[f"No maker found for '{maker_ref}'."])

    aliases = maker.aliases or []
    match = next((a for a in aliases if a == alias), None)
    if match is None:
        match = next((a for a in aliases if _normalize(a) == _normalize(alias)), None)
    if match is None:
        return UnaliasResult(
            maker.canonical_name,
            alias,
            blockers=[
                f"'{alias}' is not an alias of '{maker.canonical_name}'. "
                f"Current aliases: {aliases or 'none'}."
            ],
        )

    seed_warning = maker.canonical_name in _load_seed_names()
    if commit:
        maker.aliases = [a for a in aliases if a != match]
        session.commit()
    return UnaliasResult(
        maker.canonical_name, match, removed=commit, seed_warning=seed_warning
    )


def find_near_duplicates(session: Session, *, threshold: int = 85) -> list[DupePair]:
    """Maker pairs whose normalized canonical names score in [threshold, 100)
    by WRatio — candidates a human should eyeball for merging.
    """
    makers = session.query(Maker).all()
    normed = [(m, _normalize(m.canonical_name)) for m in makers]
    pairs: list[DupePair] = []
    for (m1, n1), (m2, n2) in combinations(normed, 2):
        score = fuzz.WRatio(n1, n2)
        if threshold <= score < 100:
            pairs.append(DupePair(score, m1.id, m1.canonical_name, m2.id, m2.canonical_name))
    pairs.sort(key=lambda p: -p.score)
    return pairs


# --- Delete (hard) -----------------------------------------------------------


@dataclass
class DeletePlan:
    maker_name: str
    mentions_deleted: int = 0
    watch_models_deleted: int = 0
    signals_deleted: int = 0
    scores_deleted: int = 0
    blockers: list[str] = field(default_factory=list)
    done: bool = False


def delete_maker(
    session: Session, maker_ref: str, *, commit: bool = False
) -> DeletePlan:
    """Hard-delete a discovered maker and everything that references it: its
    mentions, watch models, and derived signal/score rows.

    Dry-run by default. Blocks seed makers — the loader re-creates those next
    scan, so the known_makers.yaml entry must be removed instead. Note this does
    NOT prevent re-discovery: if a live article still mentions the name, the next
    scan's resolver creates the maker again. Suppress instead for recurring names.
    """
    maker = _resolve_maker(session, maker_ref)
    if maker is None:
        return DeletePlan(maker_name=maker_ref, blockers=[f"No maker found for '{maker_ref}'."])
    if maker.canonical_name in _load_seed_names():
        return DeletePlan(
            maker_name=maker.canonical_name,
            blockers=[
                f"'{maker.canonical_name}' is a seed maker — the loader would re-create "
                f"it next scan. Remove its known_makers.yaml entry instead."
            ],
        )

    mentions = session.query(Mention).filter_by(maker_id=maker.id).count()
    watch_models = session.query(WatchModel).filter_by(maker_id=maker.id).count()
    signals = session.query(Signal).filter_by(maker_id=maker.id).count()
    scores = session.query(Score).filter_by(maker_id=maker.id).count()

    if commit:
        session.query(Mention).filter_by(maker_id=maker.id).delete()
        session.query(WatchModel).filter_by(maker_id=maker.id).delete()
        session.query(Signal).filter_by(maker_id=maker.id).delete()
        session.query(Score).filter_by(maker_id=maker.id).delete()
        session.delete(maker)
        session.commit()

    return DeletePlan(
        maker_name=maker.canonical_name,
        mentions_deleted=mentions,
        watch_models_deleted=watch_models,
        signals_deleted=signals,
        scores_deleted=scores,
        done=commit,
    )


# --- Suppress (soft, reversible) ---------------------------------------------


@dataclass
class SuppressResult:
    maker_name: str
    action: str                      # "suppress" | "unsuppress"
    reason: str | None = None
    already_in_state: bool = False   # already suppressed / already visible
    blockers: list[str] = field(default_factory=list)
    done: bool = False


def suppress_maker(
    session: Session, maker_ref: str, *, reason: str | None = None, commit: bool = False
) -> SuppressResult:
    """Hide a maker from /discoveries and score-based rankings without deleting
    it. The row + mentions stay, so the resolver re-matches the name on later
    scans (no re-creation) and the action is fully reversible. Dry-run by default.
    """
    maker = _resolve_maker(session, maker_ref)
    if maker is None:
        return SuppressResult(maker_ref, "suppress", blockers=[f"No maker found for '{maker_ref}'."])

    already = bool(maker.is_suppressed)
    if commit and not already:
        maker.is_suppressed = True
        if reason is not None:
            maker.suppressed_reason = reason
        session.commit()

    return SuppressResult(
        maker_name=maker.canonical_name,
        action="suppress",
        reason=reason if reason is not None else maker.suppressed_reason,
        already_in_state=already,
        done=commit and not already,
    )


def unsuppress_maker(
    session: Session, maker_ref: str, *, commit: bool = False
) -> SuppressResult:
    """Reverse a suppression — the maker returns to /discoveries and the rankings."""
    maker = _resolve_maker(session, maker_ref)
    if maker is None:
        return SuppressResult(maker_ref, "unsuppress", blockers=[f"No maker found for '{maker_ref}'."])

    already_visible = not bool(maker.is_suppressed)
    if commit and not already_visible:
        maker.is_suppressed = False
        maker.suppressed_reason = None
        session.commit()

    return SuppressResult(
        maker_name=maker.canonical_name,
        action="unsuppress",
        already_in_state=already_visible,
        done=commit and not already_visible,
    )


def list_suppressed(session: Session) -> list[Maker]:
    """All currently-suppressed makers, ordered by name."""
    return (
        session.query(Maker)
        .filter(Maker.is_suppressed.is_(True))
        .order_by(Maker.canonical_name)
        .all()
    )
