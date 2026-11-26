"""Maintenance operations on makers: merge duplicates, fix aliases and names, find dupes.

The remedy half of the resolver's safety net (3N). The resolver auto-aliases on
a fuzzy match; when that occasionally goes wrong, or two makers slip through as
separate rows, these let an operator clean it up. The mutating operations are
dry-run by default at the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from watch_app.extraction.resolver import _normalize
from watch_app.scoring.discovery import _load_seed_names
from watch_app.storage.models import (
    BlockedMaker,
    Maker,
    Mention,
    Score,
    Signal,
    WatchModel,
)


#: Personal relationship state vocabulary (chunk B). ORTHOGONAL to suppression:
#: these capture engagement posture toward a *real* maker, not whether the maker
#: belongs in the tool. Ordered for display; None means "no posture set".
RELATIONSHIP_STATES: tuple[str, ...] = ("target", "watching", "known", "pass")
RELATIONSHIP_STATE_LABELS: dict[str, str] = {
    "target": "Target",
    "watching": "Watching",
    "known": "Known",
    "pass": "Pass",
}


def set_relationship_state(
    session: Session, maker: Maker, state: str | None, notes: str | None
) -> None:
    """Set a maker's personal relationship state and notes (single-user/local).

    `state` must be one of RELATIONSHIP_STATES, or None to clear it. `notes` is
    free text; an empty/whitespace string clears it. Raises ValueError on an
    out-of-vocabulary state. Caller owns the transaction (no commit here) so it
    composes with whatever session scope the route or CLI is using.
    """
    if state is not None and state not in RELATIONSHIP_STATES:
        raise ValueError(f"unknown relationship state {state!r}")
    maker.relationship_state = state
    maker.notes = (notes or "").strip() or None


#: Personal taste-reaction vocabulary (Layer 3a). ORTHOGONAL to BOTH suppression
#: and relationship_state: a like/dislike *taste* signal — the input the adaptive
#: re-ranking (Layer 3b) will read — not an engagement posture and not a
#: visibility control. None means "no reaction".
TASTE_REACTIONS: tuple[str, ...] = ("like", "dislike")
TASTE_REACTION_LABELS: dict[str, str] = {"like": "Like", "dislike": "Dislike"}


def set_taste_reaction(
    session: Session, maker: Maker, reaction: str | None
) -> None:
    """Set (or clear) a maker's personal taste reaction (single-user/local).

    `reaction` must be one of TASTE_REACTIONS, or None to clear it. The
    `taste_reaction_at` timestamp is stamped with the current time when the
    reaction is set and cleared to None when it's cleared — but only when the
    value actually CHANGES, so re-saving the same reaction preserves the
    original time (the "when did I first react" the data clock cares about).
    Raises ValueError on an out-of-vocabulary value. Caller owns the
    transaction (no commit here) so it composes with the route/CLI's session.
    """
    if reaction is not None and reaction not in TASTE_REACTIONS:
        raise ValueError(f"unknown taste reaction {reaction!r}")
    if reaction != maker.taste_reaction:
        maker.taste_reaction = reaction
        maker.taste_reaction_at = (
            datetime.now(timezone.utc) if reaction else None
        )


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


# --- Rename (fix a wrongly-grabbed name) --------------------------------------


@dataclass
class RenameResult:
    old_name: str
    new_name: str
    alias_kept: bool = False        # old name recorded as an alias
    alias_promoted: bool = False    # new name was one of this maker's aliases
    blockers: list[str] = field(default_factory=list)
    done: bool = False


def rename_maker(
    session: Session,
    maker_ref: str,
    new_name: str,
    *,
    keep_old_as_alias: bool = True,
    commit: bool = False,
) -> RenameResult:
    """Correct a discovered maker's canonical name, e.g. one the scan grabbed
    wrong. Mentions, scores and history hang off maker_id, so they carry over.

    With ``keep_old_as_alias`` the old name is recorded as an alias, so a later
    scan that extracts the same string resolves it here instead of re-creating
    the bad row. Turn it off when the old name was junk rather than a spelling
    of this maker — as an alias, generic text would fuzzy-match unrelated
    mentions into it. Skipped when the names are equal under normalization
    (matching is normalized, so the alias would add nothing). If the new name
    is one of this maker's own aliases, it moves out of the alias list.

    Blocks seed makers (the loader would re-create the seed name next scan —
    rename it in known_makers.yaml instead) and any new name that normalizes to
    another maker's canonical name or alias: that's a duplicate, so merge
    instead. Dry-run by default.
    """
    new_name = (new_name or "").strip()
    maker = _resolve_maker(session, maker_ref)
    if maker is None:
        return RenameResult(maker_ref, new_name, blockers=[f"No maker found for '{maker_ref}'."])
    old_name = maker.canonical_name

    def _blocked(reason: str) -> RenameResult:
        return RenameResult(old_name, new_name, blockers=[reason])

    if not new_name:
        return _blocked("The new name can't be empty.")
    if new_name == old_name:
        return _blocked(f"'{old_name}' already has that name.")
    if old_name in _load_seed_names():
        return _blocked(
            f"'{old_name}' is a seed maker — the loader would re-create it under "
            f"its seed name next scan. Rename it in known_makers.yaml instead."
        )

    target = _normalize(new_name)
    for other in session.query(Maker).filter(Maker.id != maker.id):
        if any(_normalize(n) == target for n in [other.canonical_name, *(other.aliases or [])]):
            return _blocked(
                f"'{new_name}' already belongs to '{other.canonical_name}' "
                f"(id {other.id}) — that's a duplicate, not a rename. Merge this "
                f"maker into it from its detail page instead."
            )

    current_aliases = list(maker.aliases or [])
    aliases = [a for a in current_aliases if a != new_name]
    alias_promoted = len(aliases) != len(current_aliases)
    alias_kept = (
        keep_old_as_alias
        and _normalize(old_name) != target
        and old_name not in aliases
    )

    if commit:
        maker.canonical_name = new_name
        maker.aliases = [*aliases, old_name] if alias_kept else aliases
        session.commit()

    return RenameResult(
        old_name=old_name,
        new_name=new_name,
        alias_kept=alias_kept,
        alias_promoted=alias_promoted,
        done=commit,
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
    names_blocked: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    done: bool = False


def delete_maker(
    session: Session, maker_ref: str, *, commit: bool = False
) -> DeletePlan:
    """Hard-delete a discovered maker and everything that references it: its
    mentions, watch models, and derived signal/score rows.

    Dry-run by default. Blocks seed makers — the loader re-creates those next
    scan, so the known_makers.yaml entry must be removed instead.

    On commit, the maker's canonical name + aliases are written to the
    blocked_makers tombstone table, and the resolver refuses to re-create any
    candidate matching them (same normalize + fuzzy semantics as matching) —
    so a still-live article can't resurrect the maker on the next scan.
    Reversible via unblock_maker / `cli unblock`.
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
    names_blocked = [maker.canonical_name, *(maker.aliases or [])]

    if commit:
        session.query(Mention).filter_by(maker_id=maker.id).delete()
        session.query(WatchModel).filter_by(maker_id=maker.id).delete()
        session.query(Signal).filter_by(maker_id=maker.id).delete()
        session.query(Score).filter_by(maker_id=maker.id).delete()
        # Tombstone in the same transaction, so delete + block is atomic.
        # A re-delete after unblock + re-discovery updates the existing row
        # (canonical_name is unique) and unions any newly-learned aliases.
        existing_block = (
            session.query(BlockedMaker)
            .filter_by(canonical_name=maker.canonical_name)
            .one_or_none()
        )
        if existing_block is not None:
            existing_block.aliases = _merge_name_lists(
                existing_block.aliases, maker.aliases
            )
            existing_block.blocked_at = datetime.now(timezone.utc)
        else:
            session.add(
                BlockedMaker(
                    canonical_name=maker.canonical_name,
                    aliases=list(maker.aliases or []),
                    reason=f"deleted maker '{maker.canonical_name}'",
                )
            )
        session.delete(maker)
        session.commit()

    return DeletePlan(
        maker_name=maker.canonical_name,
        mentions_deleted=mentions,
        watch_models_deleted=watch_models,
        signals_deleted=signals,
        scores_deleted=scores,
        names_blocked=names_blocked,
        done=commit,
    )


def _merge_name_lists(existing: list[str] | None, incoming: list[str] | None) -> list[str]:
    """Union of two name lists, preserving order and dropping duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in (existing or [], incoming or []):
        for name in lst:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


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


# --- Blocklist (tombstones from hard deletes) ---------------------------------


@dataclass
class UnblockResult:
    blocked_name: str
    aliases_freed: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    done: bool = False


def unblock_maker(
    session: Session, name_ref: str, *, commit: bool = False
) -> UnblockResult:
    """Remove a blocked_makers tombstone so the name can be re-discovered.

    `name_ref` is the tombstone id or the blocked canonical name (exact, then
    normalized as a fallback). Removes the whole tombstone — canonical name and
    all its blocked aliases together. Dry-run by default.
    """
    row = None
    if name_ref.isdigit():
        row = session.query(BlockedMaker).filter_by(id=int(name_ref)).first()
    if row is None:
        row = (
            session.query(BlockedMaker)
            .filter_by(canonical_name=name_ref)
            .one_or_none()
        )
    if row is None:
        target = _normalize(name_ref)
        row = next(
            (
                b
                for b in session.query(BlockedMaker).all()
                if _normalize(b.canonical_name) == target
            ),
            None,
        )
    if row is None:
        return UnblockResult(
            blocked_name=name_ref,
            blockers=[f"No blocked name found for '{name_ref}'."],
        )

    aliases = list(row.aliases or [])
    if commit:
        session.delete(row)
        session.commit()

    return UnblockResult(
        blocked_name=row.canonical_name, aliases_freed=aliases, done=commit
    )


def list_blocked(session: Session) -> list[BlockedMaker]:
    """All blocked-name tombstones, ordered by name."""
    return (
        session.query(BlockedMaker).order_by(BlockedMaker.canonical_name).all()
    )
