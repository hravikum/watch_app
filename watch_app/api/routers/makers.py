"""Makers router.

Serves:
- GET /                    → full dashboard page
- GET /makers/top          → HTMX fragment, ranked list (swappable by preset)
- GET /makers/{maker_id}   → maker detail page
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from watch_app.config import get_settings
from watch_app import taste
from watch_app.extraction.resolver import _normalize
from watch_app.extraction.maker_admin import (
    RELATIONSHIP_STATES,
    RELATIONSHIP_STATE_LABELS,
    TASTE_REACTIONS,
    TASTE_REACTION_LABELS,
    delete_maker,
    find_near_duplicates,
    merge_makers,
    rename_maker,
    set_relationship_state,
    set_taste_reaction,
    suppress_maker,
    unsuppress_maker,
)
from watch_app.scoring.methodology import (
    SIGNAL_DESCRIPTIONS,
    build_source_contributions,
    build_source_detail,
    build_worked_example,
    build_worked_example_for_maker,
)
from watch_app.scoring.affinity import AFFINITY_WEIGHT, build_for_you_ranking
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.queries import fetch_top_makers, fetch_all_makers_comparison, fetch_rank_changes, fetch_mention_volume, fetch_rank_trajectories, latest_scores_query
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Signal, Source


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


router = APIRouter()


# --- Helpers ---


def _fetch_top_for_template(
    preset_name: str, limit: int = 20, state: str | None = None
) -> list[dict]:
    """Wrap fetch_top_makers with the field name the template expects (`name`).

    fetch_top_makers returns canonical_name; template uses name. Also preserves
    maker_id for row click-throughs.

    When `state` is one of RELATIONSHIP_STATES, restrict the ranked list to
    makers in that personal state (ranked within it). This is a ranking view,
    so makers in the state that have no score yet won't appear — set state on
    the detail page; the dashboard surfaces the ones that are also ranked.
    """
    fetch_limit = 100_000 if state in RELATIONSHIP_STATES else limit
    with session_scope() as session:
        rows = fetch_top_makers(session, preset_name, limit=fetch_limit)
    if state in RELATIONSHIP_STATES:
        rows = [r for r in rows if r.get("relationship_state") == state][:limit]
    return [
        {
            "maker_id": r["maker_id"],
            "rank": r["rank"],
            "name": r["canonical_name"],
            "country": r["country"],
            "score": r["score"],
            "mention_count": r["mention_count"],
            "relationship_state": r.get("relationship_state"),
        }
        for r in rows
    ]


def _fetch_maker_detail(maker_id: int) -> dict | None:
    """Collect everything needed for a maker detail page — in one session.

    Returns a plain dict so the template doesn't hold any ORM references.
    Returns None if the maker doesn't exist.
    """
    with session_scope() as session:
        maker = session.get(Maker, maker_id)
        if maker is None:
            return None

        # Latest score under each preset — one row per preset.
        scores_by_preset: dict[str, dict] = {}
        for preset_name in PRESETS:
            stmt, score_alias = latest_scores_query(preset_name)
            stmt = stmt.where(score_alias.maker_id == maker_id)
            row = session.execute(stmt).scalar_one_or_none()
            if row is not None:
                scores_by_preset[preset_name] = {
                    "value": row.value,
                    "rank": row.rank,
                    "computed_at": row.computed_at,
                }

        # Latest signals — one row per signal_type, the most recent value.
        # Use a GROUP BY trick: subquery finds max computed_at per type, then
        # join back.
        latest_signal_times = (
            session.query(
                Signal.signal_type,
                func.max(Signal.computed_at).label("max_at"),
            )
            .filter(Signal.maker_id == maker_id)
            .group_by(Signal.signal_type)
            .subquery()
        )
        signal_rows = (
            session.query(Signal)
            .join(
                latest_signal_times,
                (Signal.signal_type == latest_signal_times.c.signal_type)
                & (Signal.computed_at == latest_signal_times.c.max_at),
            )
            .filter(Signal.maker_id == maker_id)
            .all()
        )
        signals = {s.signal_type: s.value for s in signal_rows}

        # All mentions, most recent first.
        mentions = (
            session.query(Mention)
            .options(joinedload(Mention.source))
            .filter(Mention.maker_id == maker_id)
            .order_by(
                func.coalesce(Mention.published_at, Mention.discovered_at).desc()
            )
            .all()
        )

        mention_list = [
            {
                "url": m.url,
                "title": m.title,
                "excerpt": m.excerpt,
                "source_name": m.source.name if m.source else "—",
                "source_prestige": m.source.prestige_weight if m.source else None,
                "published_at": m.published_at,
                "discovered_at": m.discovered_at,
            }
            for m in mentions
        ]

        # GPHG-nominated watch models, ordered most recent year first then by
        # name. The maker detail page renders these as a structured catalog.
        from watch_app.storage.models import WatchModel

        model_rows = (
            session.query(WatchModel)
            .filter(WatchModel.maker_id == maker_id)
            .order_by(WatchModel.gphg_year.desc(), WatchModel.name.asc())
            .all()
        )
        models = [
            {
                "id": m.id,
                "name": m.name,
                "gphg_year": m.gphg_year,
                "gphg_url": m.gphg_url,
                "image_url": m.image_url,
                "case_material": m.case_material,
                "case_diameter_mm": m.case_diameter_mm,
                "movement_caliber": m.movement_caliber,
                "movement_type": m.movement_type,
                "frequency_hz": m.frequency_hz,
                "power_reserve_hours": m.power_reserve_hours,
                "water_resistance_m": m.water_resistance_m,
                "complications": m.complications or [],
                "price_chf": m.price_chf,
                "limited_edition_count": m.limited_edition_count,
                "description": m.description,
            }
            for m in model_rows
        ]

        return {
            "id": maker.id,
            "canonical_name": maker.canonical_name,
            "aliases": maker.aliases or [],
            "country": maker.country,
            "founded_year": maker.founded_year,
            "website": maker.website,
            "is_independent": maker.is_independent,
            "is_suppressed": bool(maker.is_suppressed),
            "suppressed_reason": maker.suppressed_reason,
            "first_seen_at": maker.first_seen_at,
            "relationship_state": maker.relationship_state,
            "notes": maker.notes,
            "taste_reaction": maker.taste_reaction,
            "taste_reaction_at": maker.taste_reaction_at,
            "scores": scores_by_preset,
            "signals": signals,
            "mentions": mention_list,
            "models": models,
            "profile": _build_profile(maker),
        }


def _build_profile(maker: Maker) -> list[dict]:
    """Build the display rows for the maker profile panel.

    One row per taste dimension that has a value, in TASTE_FIELDS order, with
    tokens turned into human-readable labels. Returns [] for an untagged maker
    so the template omits the panel entirely.
    """
    rows: list[dict] = []
    for field in taste.TASTE_FIELDS:
        value = getattr(maker, field)
        if not value:
            continue
        if field in taste.MULTI_VALUE_FIELDS:
            display_values = [taste.display_label(v) for v in value]
        else:
            display_values = [taste.display_label(value)]
        rows.append({"label": taste.FIELD_LABELS[field], "tags": display_values})
    return rows


# --- Routes ---


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, state: str = Query(None)) -> HTMLResponse:
    """Render the full dashboard page.

    `state` (one of RELATIONSHIP_STATES) filters the ranking to makers in that
    personal relationship state; absent/unknown shows the full top list.
    """
    settings = get_settings()
    preset = settings.default_preset
    current_state = state if state in RELATIONSHIP_STATES else None
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "presets": list(PRESETS.keys()),
            "current_preset": preset,
            "makers": _fetch_top_for_template(preset, state=current_state),
            "relationship_states": list(RELATIONSHIP_STATES),
            "state_labels": RELATIONSHIP_STATE_LABELS,
            "current_state": current_state,
        },
    )


@router.get("/makers/top", response_class=HTMLResponse)
def top_fragment(
    request: Request,
    preset: str = Query(...),
    state: str = Query(None),
) -> HTMLResponse:
    """HTMX endpoint: returns just the <table> fragment.

    Called when the user clicks a preset toggle or a relationship-state filter;
    the page swaps this fragment into place without a full reload.
    """
    if preset not in PRESETS:
        preset = get_settings().default_preset
    current_state = state if state in RELATIONSHIP_STATES else None
    return templates.TemplateResponse(
        request=request,
        name="_makers_table.html",
        context={
            "makers": _fetch_top_for_template(preset, state=current_state),
            "current_preset": preset,
        },
    )


@router.get("/makers/{maker_id}", response_class=HTMLResponse)
def maker_detail(
    request: Request,
    maker_id: int,
    confirm: str = Query(None),
    into: str = Query(None),
) -> HTMLResponse:
    """Render the per-maker detail page.

    `confirm=delete` (admin only) renders an in-page delete confirmation panel
    populated by a delete dry-run — the exact counts that would be removed —
    before anything is committed. A seed maker shows the blocker instead of a
    confirm button.

    `confirm=merge&into=<ref>` (admin only) does the same for a merge: a dry-run
    of folding this maker into the target, shown before committing. Admin views
    also get near-duplicate merge suggestions for this maker.
    """
    detail = _fetch_maker_detail(maker_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Maker {maker_id} not found")
    admin = get_settings().admin_mode
    delete_preview = None
    merge_preview = None
    merge_suggestions: list[dict] = []
    if admin and confirm == "delete":
        with session_scope() as session:
            delete_preview = delete_maker(session, str(maker_id), commit=False)
    if admin and confirm == "merge" and into:
        with session_scope() as session:
            merge_preview = merge_makers(session, str(maker_id), into, commit=False)
    if admin:
        merge_suggestions = _merge_suggestions(maker_id)
    # Per-maker scoring breakdown under each preset: raw → z → weight →
    # contribution → composite, for THIS maker (not just the global #1 shown on
    # /methodology). None for a preset means the maker has no signals yet.
    with session_scope() as session:
        breakdowns = {
            preset_name: build_worked_example_for_maker(
                session, preset_name, maker_id
            )
            for preset_name in PRESETS
        }
    return templates.TemplateResponse(
        request=request,
        name="maker_detail.html",
        context={
            "m": detail,
            "similar": _similar_makers(maker_id),
            "breakdowns": breakdowns,
            "relationship_states": list(RELATIONSHIP_STATES),
            "state_labels": RELATIONSHIP_STATE_LABELS,
            "taste_reactions": list(TASTE_REACTIONS),
            "reaction_labels": TASTE_REACTION_LABELS,
            "admin_mode": admin,
            "delete_preview": delete_preview,
            "merge_preview": merge_preview,
            "merge_into": into,
            "merge_suggestions": merge_suggestions,
        },
    )


@router.post("/makers/{maker_id}/state")
def set_maker_state(
    maker_id: int,
    state: str = Form(default=""),
    notes: str = Form(default=""),
    reaction: str = Form(default=""),
) -> RedirectResponse:
    """Set a maker's personal relationship state, notes, and taste reaction.

    Not admin-gated — this is the core personal-tool action. `state` is one of
    RELATIONSHIP_STATES or "" to clear it; `reaction` is one of TASTE_REACTIONS
    or "" to clear it. State and reaction are orthogonal (engagement posture vs
    taste signal) but share this one "Your status" form. An out-of-vocabulary
    value for either is a 400. POST-redirect-GET back to the detail page.
    """
    chosen = state or None
    chosen_reaction = reaction or None
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).first()
        if m is None:
            raise HTTPException(status_code=404)
        try:
            set_relationship_state(session, m, chosen, notes)
            set_taste_reaction(session, m, chosen_reaction)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        session.commit()
    return RedirectResponse(url=f"/makers/{maker_id}", status_code=303)


# --- Maker maintenance: suppress / delete (admin-only, chunk 4D) ---
#
# Surfaces the maker_admin operations (CLI since 3O) in the UI. Admin-gated like
# /review and promote — these are curation/maintenance, distinct from the
# personal-posture actions (state/reaction) that POST /state handles and that
# stay ungated. Suppress is reversible (303 back to the page); delete is the
# hard path, so it goes through the in-page confirm panel (GET ?confirm=delete,
# a dry-run) before this POST commits, then redirects to /discoveries since the
# row is gone. The dry-run/commit semantics and the seed guard live in
# maker_admin; the routes just translate blockers into HTTP status codes.


@router.post("/makers/{maker_id}/suppress")
def suppress_maker_route(
    maker_id: int, reason: str = Form(default="")
) -> RedirectResponse:
    """Hide a maker from discoveries/rankings without deleting it. Reversible."""
    _require_admin()
    with session_scope() as session:
        res = suppress_maker(
            session, str(maker_id), reason=reason.strip() or None, commit=True
        )
        if res.blockers:
            raise HTTPException(status_code=404, detail=" ".join(res.blockers))
    return RedirectResponse(url=f"/makers/{maker_id}", status_code=303)


@router.post("/makers/{maker_id}/unsuppress")
def unsuppress_maker_route(maker_id: int) -> RedirectResponse:
    """Reverse a suppression — the maker returns to discoveries/rankings."""
    _require_admin()
    with session_scope() as session:
        res = unsuppress_maker(session, str(maker_id), commit=True)
        if res.blockers:
            raise HTTPException(status_code=404, detail=" ".join(res.blockers))
    return RedirectResponse(url=f"/makers/{maker_id}", status_code=303)


@router.post("/makers/{maker_id}/delete")
def delete_maker_route(maker_id: int) -> RedirectResponse:
    """Hard-delete a maker and everything referencing it. The in-page confirm
    panel (GET ?confirm=delete) shows the dry-run impact first. Seed makers are
    blocked — the loader would re-create them — and surface as 409.
    """
    _require_admin()
    with session_scope() as session:
        plan = delete_maker(session, str(maker_id), commit=True)
        if plan.blockers:
            raise HTTPException(status_code=409, detail=" ".join(plan.blockers))
    return RedirectResponse(url="/discoveries", status_code=303)


# --- Merge into another maker (admin-only, chunk 4E) ---
#
# The remedy for "this isn't a new brand" — a duplicate or name variant of one
# already tracked. merge_makers (CLI since 3N) reassigns mentions/watch-models
# to the target, folds names in as aliases, drops derived rows, and deletes the
# source. The UI offers two ways to pick the target: near-duplicate suggestions
# (find_near_duplicates filtered to this maker — the common near-name case) and
# a typeahead fallback over all makers. Both lead to the same confirm panel
# (GET ?confirm=merge&into=…, a dry-run) before this POST commits. Seed makers
# are blocked as the *source* only; any maker (seed included) is a valid target.


def _merge_suggestions(maker_id: int, limit: int = 5) -> list[dict]:
    """Near-duplicate makers for this one — likely merge targets, with score.

    Thin filter over find_near_duplicates (all fuzzy name pairs DB-wide) down to
    pairs involving this maker, returning the other side. The "isn't a new
    brand" case is usually a near-name match, so this surfaces the target
    without typing. Admin-only at the call site (O(N²) over makers).
    """
    out: list[dict] = []
    with session_scope() as session:
        for p in find_near_duplicates(session):
            if p.a_id == maker_id:
                other_id, other_name = p.b_id, p.b_name
            elif p.b_id == maker_id:
                other_id, other_name = p.a_id, p.a_name
            else:
                continue
            out.append(
                {"maker_id": other_id, "name": other_name, "score": round(p.score)}
            )
            if len(out) >= limit:
                break
    return out


@router.get("/makers/{maker_id}/merge/search", response_class=HTMLResponse)
def merge_search(
    request: Request, maker_id: int, q: str = Query("")
) -> HTMLResponse:
    """HTMX typeahead: makers whose name or alias matches `q`, as merge targets.

    The fallback for when the near-duplicate suggestions don't cover the case.
    Case-insensitive substring over canonical_name + aliases, excluding this
    maker itself; seed makers are included since they're valid targets (merge
    only blocks a seed as the *source*). Needs ≥2 chars; name-ordered, capped.
    """
    _require_admin()
    needle = q.strip().lower()
    results: list[dict] = []
    if len(needle) >= 2:
        with session_scope() as session:
            for m in (
                session.query(Maker).order_by(Maker.canonical_name).all()
            ):
                if m.id == maker_id:
                    continue
                hay = [m.canonical_name.lower()]
                hay += [(a or "").lower() for a in (m.aliases or [])]
                if any(needle in h for h in hay):
                    results.append(
                        {
                            "maker_id": m.id,
                            "name": m.canonical_name,
                            "country": m.country,
                        }
                    )
                    if len(results) >= 10:
                        break
    return templates.TemplateResponse(
        request=request,
        name="_merge_results.html",
        context={"results": results, "maker_id": maker_id, "q": q.strip()},
    )


@router.post("/makers/{maker_id}/merge")
def merge_maker_route(maker_id: int, into: str = Form(...)) -> RedirectResponse:
    """Merge this maker into the target (`into`, an id or name); redirect to the
    survivor. The in-page confirm panel (GET ?confirm=merge&into=…) shows the
    dry-run impact first. Blockers (seed source, self-merge, missing target)
    surface as 409.
    """
    _require_admin()
    with session_scope() as session:
        plan = merge_makers(session, str(maker_id), into, commit=True)
        if plan.blockers:
            raise HTTPException(status_code=409, detail=" ".join(plan.blockers))
        survivor = (
            session.query(Maker)
            .filter_by(canonical_name=plan.into_name)
            .first()
        )
        survivor_id = survivor.id if survivor else None
    target = f"/makers/{survivor_id}" if survivor_id else "/"
    return RedirectResponse(url=target, status_code=303)


# --- Triage (Layer 3a workflow, chunk 3X) ---


def _fetch_triage_queue() -> list[dict]:
    """The triage queue: every untriaged maker (``taste_reaction`` unset),
    suppressed makers excluded.

    Scored makers come first in rank order under the default preset, then the
    unscored ones newest-first — so the highest-signal calls sit on top while
    the long tail of unranked makers stays reachable. Each row carries just
    enough to make a taste call without leaving the page (rank/score, type,
    aesthetic tags). Reacting (POST /triage/{id}) drops a maker from this set.
    """
    preset = get_settings().default_preset
    with session_scope() as session:
        ranking = fetch_top_makers(session, preset, limit=100_000)
        rank_by_id = {r["maker_id"]: r for r in ranking}
        makers = (
            session.query(Maker)
            .filter(Maker.taste_reaction.is_(None), Maker.is_suppressed.is_(False))
            .order_by(Maker.first_seen_at.desc())
            .all()
        )
        scored: list[dict] = []
        unscored: list[dict] = []
        for m in makers:
            r = rank_by_id.get(m.id)
            row = {
                "maker_id": m.id,
                "name": m.canonical_name,
                "country": m.country,
                "rank": r["rank"] if r else None,
                "score": r["score"] if r else None,
                "mention_count": r["mention_count"] if r else 0,
                "relationship_state": m.relationship_state,
                "maker_type": (
                    taste.display_label(m.maker_type) if m.maker_type else None
                ),
                "aesthetic": [
                    taste.display_label(t) for t in (m.aesthetic_tags or [])
                ],
            }
            (scored if r is not None else unscored).append(row)
    scored.sort(key=lambda x: x["rank"])
    # unscored is already newest-first from the query's first_seen_at order.
    return scored + unscored


@router.get("/triage", response_class=HTMLResponse)
def triage(request: Request) -> HTMLResponse:
    """Triage queue: react like/dislike to makers you haven't yet, in one pass.

    The workflow half of reaction capture — 3V shipped the per-maker control;
    this is the queue that surfaces what's left. Not admin-gated: reacting is a
    personal-tool action, same as the detail-page status form. Skipping is just
    leaving a card alone; it returns next time.
    """
    queue = _fetch_triage_queue()
    return templates.TemplateResponse(
        request=request,
        name="triage.html",
        context={
            "makers": queue,
            "count": len(queue),
            "reaction_labels": TASTE_REACTION_LABELS,
        },
    )


@router.post("/triage/{maker_id}", response_class=HTMLResponse)
def triage_react(
    request: Request,
    maker_id: int,
    reaction: str = Form(...),
) -> HTMLResponse:
    """Set a maker's taste reaction from the queue; return an HTMX fragment that
    swaps the card out and updates the remaining count.

    `reaction` must be one of TASTE_REACTIONS — the queue only offers
    like/dislike, with no "clear" (that's what the detail page is for), so an
    empty or out-of-vocabulary value is a 400 and a missing maker is a 404.
    This is deliberately separate from POST /makers/{id}/state, which
    303-redirects (right for the detail form, wrong for an inline swap).
    """
    if reaction not in TASTE_REACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown reaction {reaction!r}")
    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).first()
        if m is None:
            raise HTTPException(status_code=404)
        name = m.canonical_name
        set_taste_reaction(session, m, reaction)
        session.commit()
        remaining = (
            session.query(Maker)
            .filter(Maker.taste_reaction.is_(None), Maker.is_suppressed.is_(False))
            .count()
        )
    return templates.TemplateResponse(
        request=request,
        name="_triage_reacted.html",
        context={
            "maker_id": maker_id,
            "name": name,
            "reaction_label": TASTE_REACTION_LABELS[reaction],
            "remaining": remaining,
        },
    )


_COMPARISON_SORT_KEYS = {
    "rank_a": ("rank_a", False),  # ascending
    "rank_b": ("rank_b", False),
    "name": ("canonical_name", False),
    "mentions": ("mention_count", True),  # descending — more mentions first
    "divergence": ("divergence", True),
}


@router.get("/comparison", response_class=HTMLResponse)
def comparison(
    request: Request,
    sort: str = Query("rank_a"),
) -> HTMLResponse:
    """Render the side-by-side preset-comparison table.

    Shows every maker with at least one preset score, both presets' ranks
    and scores, and the rank divergence. Default sort is ascending
    rank_a (rising_star). Click any column header to re-sort.

    Sort parameter validates against a fixed allow-list — unknown values
    fall back to the default — to avoid SQL injection by way of column
    name and to keep the URL surface small.
    """
    sort_key, descending = _COMPARISON_SORT_KEYS.get(
        sort, _COMPARISON_SORT_KEYS["rank_a"]
    )

    preset_a = "rising_star"
    preset_b = "hidden_gem"
    with session_scope() as session:
        rows = fetch_all_makers_comparison(session, preset_a, preset_b)

    # Sort. Rows whose key is None always sort last regardless of direction —
    # we never want to bury a maker because they happen to lack one preset.
    def sort_value(row):
        v = row[sort_key]
        if v is None:
            return (1, 0)
        return (0, -v if descending and isinstance(v, (int, float)) else v)

    if descending and sort_key == "canonical_name":
        rows.sort(
            key=lambda r: (r[sort_key] is None, r[sort_key] or ""),
            reverse=True,
        )
    else:
        rows.sort(key=sort_value)

    return templates.TemplateResponse(
        request=request,
        name="comparison.html",
        context={
            "rows": rows,
            "preset_a": preset_a,
            "preset_b": preset_b,
            "current_sort": sort,
            "total_count": len(rows),
        },
    )


_VALID_DAYS = (3, 7, 14)
_VALID_MOVERS_PRESETS = ("rising_star", "hidden_gem")


@router.get("/movers", response_class=HTMLResponse)
def movers(
    request: Request,
    preset: str = Query("rising_star"),
    days: int = Query(7),
) -> HTMLResponse:
    """Render the rank-change page.

    Shows climbers, fallers, and new entrants over the last `days` days
    under the chosen preset. Both `preset` and `days` are validated
    against allow-lists; unrecognized values fall back to defaults.

    Pages without enough history (`days` ago is before the first scan
    that wrote scores) will show no climbers or fallers but every
    currently-ranked maker as a new entrant. The template makes this
    case visible rather than confusing.
    """
    if preset not in _VALID_MOVERS_PRESETS:
        preset = "rising_star"
    if days not in _VALID_DAYS:
        days = 7

    with session_scope() as session:
        groups = fetch_rank_changes(session, preset, days=days)

    return templates.TemplateResponse(
        request=request,
        name="movers.html",
        context={
            "preset": preset,
            "presets": _VALID_MOVERS_PRESETS,
            "days": days,
            "valid_days": _VALID_DAYS,
            "climbers": groups["climbers"],
            "fallers": groups["fallers"],
            "new_entrants": groups["new_entrants"],
            "has_any_movement": (
                bool(groups["climbers"]) or bool(groups["fallers"])
            ),
        },
    )


@router.get("/digest", response_class=HTMLResponse)
def digest(
    request: Request,
    preset: str = Query("rising_star"),
    days: int = Query(7),
) -> HTMLResponse:
    """Render the weekly "what changed" digest.

    New discoveries, movers, and activity on watched/target makers over the
    last `days` days under `preset`. Both inputs are validated against the
    movers allow-lists; unknown values fall back to defaults. The template
    surfaces the thin-history case (no baseline older than the window) so a
    provisional digest isn't mistaken for a quiet week.
    """
    if preset not in _VALID_MOVERS_PRESETS:
        preset = "rising_star"
    if days not in _VALID_DAYS:
        days = 7

    from watch_app.scoring.digest import compute_weekly_digest

    with session_scope() as session:
        d = compute_weekly_digest(session, window_days=days, preset=preset)

    return templates.TemplateResponse(
        request=request,
        name="digest.html",
        context={
            "preset": preset,
            "presets": _VALID_MOVERS_PRESETS,
            "days": days,
            "valid_days": _VALID_DAYS,
            "digest": d,
        },
    )


# --- Trends: server-side SVG chart geometry -------------------------------
#
# These turn the trend data series into ready-to-render SVG coordinates so the
# template stays declarative (just draws polylines/paths). Kept here with the
# other view helpers; pure functions, unit-tested directly. No client JS.

_CHART_W = 720

#: Distinguishable but warm-leaning line colors for trajectory makers.
_TRAJECTORY_COLORS = [
    "#d97706", "#0d9488", "#e11d48", "#4f46e5", "#65a30d",
    "#c2410c", "#0e7490", "#a21caf", "#64748b", "#1d4ed8",
]


def _date_label(d) -> str:
    """Portable 'Mon D' label (avoids platform-specific %-d)."""
    return f"{d:%b} {d.day}"


def _build_volume_chart(volume: list[dict]) -> dict | None:
    """Geometry for the daily mention-volume line. None when there's no data."""
    if not volume:
        return None

    w, h = _CHART_W, 220
    pl, pr, pt, pb = 44, 14, 16, 30
    plot_w = w - pl - pr
    plot_h = h - pt - pb
    n = len(volume)
    y_max = max(max((d["count"] for d in volume), default=0), 1)

    def x_at(i: int) -> float:
        return pl + plot_w / 2 if n == 1 else pl + (i / (n - 1)) * plot_w

    def y_at(c: float) -> float:
        return pt + (1 - c / y_max) * plot_h

    pts = [(x_at(i), y_at(d["count"])) for i, d in enumerate(volume)]
    baseline = pt + plot_h
    points = [
        {"x": round(x, 1), "y": round(y, 1),
         "label": _date_label(volume[i]["date"]), "count": volume[i]["count"]}
        for i, (x, y) in enumerate(pts)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (
        f"M {pts[0][0]:.1f},{baseline:.1f} "
        + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts)
        + f" L {pts[-1][0]:.1f},{baseline:.1f} Z"
    )

    y_ticks = [
        {"y": round(y_at(v), 1), "label": str(v)}
        for v in sorted({0, y_max // 2, y_max})
    ]

    n_labels = min(n, 5)
    if n_labels <= 1:
        idxs = [0]
    else:
        idxs = sorted({round(k * (n - 1) / (n_labels - 1)) for k in range(n_labels)})
    x_ticks = [
        {"x": round(x_at(i), 1), "label": _date_label(volume[i]["date"])}
        for i in idxs
    ]

    return {
        "w": w, "h": h, "pl": pl, "pr": pr, "pt": pt,
        "baseline": round(baseline, 1),
        "line": line, "area": area, "points": points,
        "y_ticks": y_ticks, "x_ticks": x_ticks, "y_max": y_max,
    }


def _rank_ticks(max_rank: int) -> list[int]:
    """A few sensible rank gridlines between 1 and max_rank (inclusive)."""
    if max_rank <= 1:
        return [1]
    step = max(1, round(max_rank / 4))
    return sorted({1, max_rank} | set(range(1, max_rank + 1, step)))


def _build_trajectory_chart(trajectories: dict) -> dict | None:
    """Geometry for the rank-over-time lines. None when nothing is plottable.

    Rank axis is inverted: #1 sits at the top. Each maker becomes one polyline
    plus an end marker (so a single-point trajectory is still visible).
    """
    plotted = [m for m in trajectories.get("makers", []) if m["points"]]
    if not plotted:
        return None

    w, h = _CHART_W, 300
    pl, pr, pt, pb = 44, 14, 16, 30
    plot_w = w - pl - pr
    plot_h = h - pt - pb
    max_rank = max(trajectories.get("max_rank", 1), 1)

    all_ts = [p["computed_at"] for m in plotted for p in m["points"]]
    tmin, tmax = min(all_ts), max(all_ts)
    span = (tmax - tmin).total_seconds()

    def x_at(t) -> float:
        return pl + plot_w / 2 if span <= 0 else pl + ((t - tmin).total_seconds() / span) * plot_w

    def y_at(rank: int) -> float:
        return pt if max_rank == 1 else pt + ((rank - 1) / (max_rank - 1)) * plot_h

    lines = []
    for idx, m in enumerate(plotted):
        coords = [(x_at(p["computed_at"]), y_at(p["rank"])) for p in m["points"]]
        lines.append({
            "name": m["canonical_name"],
            "maker_id": m["maker_id"],
            "current_rank": m["current_rank"],
            "color": _TRAJECTORY_COLORS[idx % len(_TRAJECTORY_COLORS)],
            "poly": " ".join(f"{x:.1f},{y:.1f}" for x, y in coords),
            "points": [
                {"x": round(cx, 1), "y": round(cy, 1),
                 "label": _date_label(p["computed_at"]), "rank": p["rank"]}
                for (cx, cy), p in zip(coords, m["points"])
            ],
            "end_x": round(coords[-1][0], 1),
            "end_y": round(coords[-1][1], 1),
        })

    y_ticks = [
        {"y": round(y_at(r), 1), "label": f"#{r}"}
        for r in _rank_ticks(max_rank)
    ]

    if span <= 0:
        x_ticks = [{"x": round(pl + plot_w / 2, 1), "label": _date_label(tmin)}]
    else:
        x_ticks = [
            {
                "x": round(pl + (k / 4) * plot_w, 1),
                "label": _date_label(tmin + (tmax - tmin) * (k / 4)),
            }
            for k in range(5)
        ]

    return {
        "w": w, "h": h, "pl": pl, "pr": pr,
        "lines": lines, "y_ticks": y_ticks, "x_ticks": x_ticks, "max_rank": max_rank,
    }


@router.get("/trends", response_class=HTMLResponse)
def trends(request: Request, preset: str = Query(None)) -> HTMLResponse:
    """Render the trends page: mention volume over time + rank trajectories.

    Both charts are server-side SVG (no JS, no schema change). Volume counts
    non-awards mentions by ingest date (see fetch_mention_volume for why).
    Trajectories plot the current top-10 makers' stored rank history under the
    selected preset; an unknown preset falls back to the default.
    """
    preset = _resolve_preset(preset)

    with session_scope() as session:
        volume = fetch_mention_volume(session)
        trajectories = fetch_rank_trajectories(session, preset, top_n=10)

    return templates.TemplateResponse(
        request=request,
        name="trends.html",
        context={
            "preset": preset,
            "presets": list(PRESETS.keys()),
            "volume_chart": _build_volume_chart(volume),
            "trajectory_chart": _build_trajectory_chart(trajectories),
            "total_mentions": sum(v["count"] for v in volume),
            "n_days": len(volume),
        },
    )


@router.get("/for-you", response_class=HTMLResponse)
def for_you(request: Request, preset: str = Query(None)) -> HTMLResponse:
    """Render the taste-personalized ranking — Layer 3b, the arc's headline.

    Re-orders the chosen base preset's ranking by how each maker's taste tags
    line up with your like/dislike reactions (see scoring/affinity.py): a maker
    close to what you've liked climbs, one close to what you've disliked sinks,
    one with no taste signal holds its base spot. Derived per request from the
    reactions — no schema change, updates the moment you react. An unknown
    preset falls back to the default; with no reactions yet it points you at the
    triage queue.
    """
    preset = _resolve_preset(preset)

    with session_scope() as session:
        data = build_for_you_ranking(session, preset, display_limit=30)

    return templates.TemplateResponse(
        request=request,
        name="for_you.html",
        context={
            "preset": preset,
            "presets": list(PRESETS.keys()),
            "rows": data["rows"],
            "n_likes": data["n_likes"],
            "n_dislikes": data["n_dislikes"],
            "n_ranked": data["n_ranked"],
            "has_reactions": data["has_reactions"],
            "affinity_weight": AFFINITY_WEIGHT,
            "reaction_labels": TASTE_REACTION_LABELS,
        },
    )


@router.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request) -> HTMLResponse:
    """Render the scoring methodology page.

    Lists each signal with its formula, shows the preset weights pulled live
    from PRESETS, and includes a worked example for each preset using the
    currently top-ranked maker. If no data has been scored yet, the worked
    examples section is skipped gracefully.
    """
    with session_scope() as session:
        worked_examples = {}
        for preset_name in PRESETS:
            example = build_worked_example(session, preset_name)
            if example is not None:
                worked_examples[preset_name] = example
        source_contributions = build_source_contributions(session)

    return templates.TemplateResponse(
        request=request,
        name="methodology.html",
        context={
            "signal_descriptions": SIGNAL_DESCRIPTIONS,
            "presets": PRESETS,
            "worked_examples": worked_examples,
            "source_contributions": source_contributions,
        },
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(request: Request, source_id: int) -> HTMLResponse:
    """Per-source drill-down: metadata, contribution stats, and the makers it

    surfaced. Reached from the source-contributions table on /methodology.
    """
    with session_scope() as session:
        detail = build_source_detail(session, source_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Source {source_id} not found")
        return templates.TemplateResponse(
            request=request,
            name="source_detail.html",
            context={"s": detail},
        )


@router.get("/discoveries", response_class=HTMLResponse)
def discoveries(request: Request) -> HTMLResponse:
    """Render the auto-discovered makers view.

    Shows makers the resolver created from article mentions that aren't in
    the curated seed YAML — these are the brands your tool found that you
    didn't tell it about. Sorted by first_seen_at descending so newest
    discoveries appear at the top.

    For each maker, displays what we know (country, founded_year, website,
    independence), which fields came from LLM enrichment vs seed/manual,
    how many mentions they've accumulated, and which sources mentioned
    them. The detail page is linked for deeper investigation.

    This is the visibility layer over what the tool has already learned.
    Curation (promoting a maker to the seed YAML) is still a manual step
    via editing data/known_makers.yaml. Future chunks may add automatic
    multi-field enrichment so missing fields fill in on discovery.
    """
    from watch_app.scoring.discovery import fetch_discovered_makers

    with session_scope() as session:
        discovered = fetch_discovered_makers(session)

    return templates.TemplateResponse(
        request=request,
        name="discoveries.html",
        context={
            "discovered": discovered,
            "count": len(discovered),
            "admin_mode": get_settings().admin_mode,
        },
    )


# --- Review / edit (admin-only, chunk 3H; a queue since 4N) ---


def _require_admin() -> None:
    """Gate admin-only routes. 404 (not 403) so the page is invisible when off."""
    if not get_settings().admin_mode:
        raise HTTPException(status_code=404)


def _is_htmx(request: Request) -> bool:
    """True when the request came from an HTMX swap rather than a plain form
    post (the no-JS fallback), so a route can answer with a fragment."""
    return request.headers.get("HX-Request") == "true"


def _review_counts(session) -> tuple[int, int]:
    """(to go, reviewed) over the universe /review covers — discovered
    (non-seed), non-suppressed makers — split by whether they've been saved on
    /review yet. "To go" is the queue's header count."""
    from watch_app.scoring.discovery import _load_seed_names

    seed_names = _load_seed_names()
    to_go = reviewed = 0
    rows = session.query(Maker.canonical_name, Maker.reviewed_at).filter(
        Maker.is_suppressed.is_(False)
    )
    for name, reviewed_at in rows:
        if name in seed_names:
            continue
        if reviewed_at is None:
            to_go += 1
        else:
            reviewed += 1
    return to_go, reviewed


def _review_prelaunch_count(session) -> int:
    """Discovered, non-suppressed makers flagged pre-launch, reviewed or not —
    the /review?show=prelaunch list that keeps them findable for a later pass
    once they ship (4O)."""
    from watch_app.scoring.discovery import _load_seed_names

    seed_names = _load_seed_names()
    rows = session.query(Maker.canonical_name).filter(
        Maker.is_suppressed.is_(False), Maker.is_pre_launch.is_(True)
    )
    return sum(1 for (name,) in rows if name not in seed_names)


def _brand_targets(session) -> dict[str, tuple[int, str]]:
    """normalized name or alias -> (maker id, canonical name) over every
    non-suppressed maker, seed makers included (they're valid merge targets) —
    to resolve an audit's brand suggestion to a maker already tracked."""
    index: dict[str, tuple[int, str]] = {}
    for m in session.query(Maker).filter(Maker.is_suppressed.is_(False)):
        for name in [m.canonical_name, *(m.aliases or [])]:
            if name:
                index.setdefault(_normalize(name), (m.id, m.canonical_name))
    return index


def _brand_suggestion(m: Maker, targets: dict[str, tuple[int, str]]) -> dict | None:
    """The audit's "this name belongs to brand X" for the card: X, plus the
    maker it's already tracked as (so the card can offer the merge), or no
    target (so the card suggests renaming this one to X)."""
    if not m.audit_brand:
        return None
    target = targets.get(_normalize(m.audit_brand))
    if target is not None and target[0] == m.id:
        return None
    return {
        "name": m.audit_brand,
        "maker_id": target[0] if target else None,
        "target_name": target[1] if target else None,
    }


def _audit_source_links(m: Maker) -> list[dict]:
    """The pages the web audit relied on, as links (url, title, host) — for
    the review card and the bulk-suppress list."""
    return [
        {
            "url": s["url"],
            "title": s.get("title") or "",
            "host": urlparse(s["url"]).netloc or s["url"],
        }
        for s in (m.audit_sources or [])
        if isinstance(s, dict) and s.get("url")
    ]


def _not_a_maker_queue(session) -> list[Maker]:
    """The queue's makers the web audit judged "no" — discovered (non-seed),
    non-suppressed and not yet reviewed — alphabetical: what the
    bulk-suppress page lists, and the count on its link (4P)."""
    from watch_app.scoring.discovery import _load_seed_names

    seed_names = _load_seed_names()
    rows = session.query(Maker).filter(
        Maker.is_suppressed.is_(False),
        Maker.reviewed_at.is_(None),
        Maker.audit_verdict == "no",
    )
    return sorted(
        (m for m in rows if m.canonical_name not in seed_names),
        key=lambda m: m.canonical_name.casefold(),
    )


def _review_evidence(
    session, maker_ids: list[int], max_recent: int = 3
) -> dict[int, dict]:
    """Per-maker evidence for the review cards, batched rather than N+1: the
    mention count, distinct source names, and the most recent distinct article
    titles with links (published_at, else discovered_at — the detail page's
    order). Enough to judge "is this a real maker?" without leaving the card."""
    out = {
        mid: {"mention_count": 0, "sources": [], "recent": []} for mid in maker_ids
    }
    if not maker_ids:
        return out
    rows = (
        session.query(Mention.maker_id, Mention.title, Mention.url, Source.name)
        .join(Source, Source.id == Mention.source_id)
        .filter(Mention.maker_id.in_(maker_ids))
        .order_by(func.coalesce(Mention.published_at, Mention.discovered_at).desc())
        .all()
    )
    for maker_id, title, url, source_name in rows:
        ev = out[maker_id]
        ev["mention_count"] += 1
        if source_name not in ev["sources"]:
            ev["sources"].append(source_name)
        title = (title or "").strip()
        if (
            title
            and len(ev["recent"]) < max_recent
            and all(r["title"] != title for r in ev["recent"])
        ):
            ev["recent"].append({"title": title, "url": url, "source": source_name})
    for ev in out.values():
        ev["sources"].sort()
    return out


def _review_summary(m: Maker, auto: dict) -> tuple[list[dict], list[str]]:
    """The card's at-a-glance read of a maker's current values, so a correct
    card can be confirmed without opening the form: one entry per populated
    field (label, display text, and whether it's an unchecked "auto" value),
    plus the labels of the fields still blank. Website is left to the
    evidence line, where it's a link.

    is_independent has no blank state — it defaults to True — so an
    unreviewed maker whose independence enrichment never determined reads
    "Yes (default)" rather than passing the schema default off as a fact."""
    filled: list[dict] = []
    blank: list[str] = []
    factual = [
        ("country", "Country", m.country),
        ("founded_year", "Founded", str(m.founded_year) if m.founded_year else None),
    ]
    for field, label, text in factual:
        if text:
            filled.append({"label": label, "text": text, "auto": auto[field]})
        else:
            blank.append(label)
    if not m.is_independent:
        independent = "No"
    elif auto["is_independent"] or m.reviewed_at is not None:
        independent = "Yes"
    else:
        independent = "Yes (default)"
    filled.append(
        {
            "label": "Independent",
            "text": independent,
            "auto": auto["is_independent"],
        }
    )
    if m.is_pre_launch:
        filled.append(
            {"label": "Status", "text": "Pre-launch", "auto": auto["is_pre_launch"]}
        )
    if not m.website:
        blank.append("Website")
    for field in taste.TASTE_FIELDS:
        value = getattr(m, field)
        label = taste.FIELD_LABELS[field]
        if not value:
            blank.append(label)
            continue
        tokens = value if field in taste.MULTI_VALUE_FIELDS else [value]
        filled.append(
            {
                "label": label,
                "text": ", ".join(taste.display_label(t) for t in tokens),
                "auto": auto[field],
            }
        )
    return filled, blank


def _fetch_discovered_for_review(
    reviewed: bool = False, pre_launch: bool = False
) -> list[dict]:
    """Discovered (non-seed, non-suppressed) makers for the review grid, with
    their taste + factual fields, a value summary, and the evidence to judge
    them by.

    By default this is the queue — makers not yet saved on /review — ordered
    like /triage: ranked makers first in rank order under the default preset
    (the ones that move rankings get checked first), then the unranked tail
    newest-first — except that makers the web audit judged "no" go first, so
    the junk clears in one pass (4O). `reviewed=True` lists the
    already-reviewed makers instead, most recently reviewed first, for
    corrections; `pre_launch=True` lists the makers flagged pre-launch,
    reviewed or not, newest first. Each dict carries a per-field `auto` flag
    so the template can show what nobody has checked, plus the audit's
    verdict, reason, sources and brand suggestion when it has run.
    """
    from watch_app.scoring.discovery import _load_seed_names

    seed_names = _load_seed_names()
    tracked = taste.TASTE_FIELDS + (
        "country", "founded_year", "website", "is_independent", "is_pre_launch",
    )
    preset = get_settings().default_preset
    out: list[dict] = []
    with session_scope() as session:
        rank_by_id = {
            r["maker_id"]: r["rank"]
            for r in fetch_top_makers(session, preset, limit=100_000)
        }
        query = session.query(Maker).filter(Maker.is_suppressed.is_(False))
        if pre_launch:
            query = query.filter(Maker.is_pre_launch.is_(True)).order_by(
                Maker.first_seen_at.desc()
            )
        elif reviewed:
            query = query.filter(Maker.reviewed_at.isnot(None)).order_by(
                Maker.reviewed_at.desc()
            )
        else:
            query = query.filter(Maker.reviewed_at.is_(None)).order_by(
                Maker.first_seen_at.desc()
            )
        rows = [m for m in query.all() if m.canonical_name not in seed_names]
        evidence = _review_evidence(session, [m.id for m in rows])
        targets = _brand_targets(session) if any(m.audit_brand for m in rows) else {}
        for m in rows:
            taste_values = {f: getattr(m, f) for f in taste.TASTE_FIELDS}
            auto = {f: (f in (m.enriched_fields or [])) for f in tracked}
            summary, blank = _review_summary(m, auto)
            out.append(
                {
                    "id": m.id,
                    "canonical_name": m.canonical_name,
                    "country": m.country,
                    "founded_year": m.founded_year,
                    "website": m.website,
                    "website_host": (
                        (urlparse(m.website).netloc or m.website) if m.website else None
                    ),
                    "is_independent": m.is_independent,
                    "taste": taste_values,
                    "has_taste": any(taste_values.values()),
                    "auto": auto,
                    "rank": rank_by_id.get(m.id),
                    "summary": summary,
                    "blank": blank,
                    "pre_launch": bool(m.is_pre_launch),
                    "verdict": m.audit_verdict,
                    "reason": m.audit_reason,
                    "audit_sources": _audit_source_links(m),
                    "brand": _brand_suggestion(m, targets),
                    **evidence[m.id],
                }
            )
    if not reviewed and not pre_launch:
        # Audited "no" first; then ranked makers, in rank order; the unranked
        # tail keeps the query's newest-first order (the sort is stable).
        out.sort(
            key=lambda d: (d["verdict"] != "no", d["rank"] is None, d["rank"] or 0)
        )
    return out


@router.get("/review", response_class=HTMLResponse)
def review(
    request: Request,
    confirm: str = Query(None),
    maker: int = Query(None),
    show: str = Query(None),
    suppressed: int = Query(None),
    skipped: int = Query(None),
) -> HTMLResponse:
    """Admin review queue for discovered makers (chunk 3H; a queue since 4N).

    Lists the auto-discovered (non-seed) makers nobody has reviewed yet, each
    card leading with its current values (fields filled by LLM enrichment
    flagged "auto") and the evidence it was discovered on, with the seven taste
    dimensions plus the factual fields editable behind an Edit toggle. Saving
    is the sign-off: edits overwrite the drafts, the "auto" flags clear,
    `reviewed_at` is stamped, and the maker leaves the queue — enrichment never
    touches it again. `show=reviewed` lists the reviewed makers instead, for
    corrections, and `show=prelaunch` the makers flagged pre-launch (4O). Once
    `cli enrich --audit` has run, cards also carry its verdict, reason,
    sources and brand suggestion. Seed makers are intentionally absent — their
    taste is curated in the YAML. Gated by WATCH_APP_ADMIN.

    `confirm=delete&maker=<id>` renders an in-card delete confirmation populated
    by a delete dry-run — the exact counts that would be removed — before the
    POST commits, mirroring the detail page's panel. A seed maker shows the
    blocker instead of a confirm button. (With HTMX the card fetches the same
    panel from /review/confirm-delete/<id> instead; this is the no-JS path.)

    The header links the bulk-suppress page when the audit has judged queue
    makers "no" (4P); `suppressed` / `skipped` are that page's result, shown
    as a notice after its redirect.
    """
    _require_admin()
    show_reviewed = show == "reviewed"
    show_prelaunch = show == "prelaunch"
    makers = _fetch_discovered_for_review(
        reviewed=show_reviewed, pre_launch=show_prelaunch
    )
    with session_scope() as session:
        to_go, reviewed_count = _review_counts(session)
        prelaunch_count = _review_prelaunch_count(session)
        not_a_maker_count = len(_not_a_maker_queue(session))
    delete_preview = None
    if confirm == "delete" and maker is not None:
        with session_scope() as session:
            delete_preview = delete_maker(session, str(maker), commit=False)
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "makers": makers,
            "count": len(makers),
            "to_go": to_go,
            "reviewed_count": reviewed_count,
            "show_reviewed": show_reviewed,
            "prelaunch_count": prelaunch_count,
            "show_prelaunch": show_prelaunch,
            "not_a_maker_count": not_a_maker_count,
            "bulk_suppressed": suppressed,
            "bulk_skipped": skipped or 0,
            "vocabulary": taste.VOCABULARY,
            "field_labels": taste.FIELD_LABELS,
            "multi_fields": list(taste.MULTI_VALUE_FIELDS),
            "single_fields": list(taste.SINGLE_VALUE_FIELDS),
            "display_label": taste.display_label,
            "delete_preview": delete_preview,
            "confirm_maker_id": maker,
        },
    )


def _review_done(
    request: Request,
    maker_id: int,
    name: str,
    action: str,
    to_go: int,
    link: bool = True,
) -> HTMLResponse:
    """HTMX answer to a successful card action: a slim ✓ row that replaces the
    card (a trail of what this pass did) plus an out-of-band update of the "to
    go" count. `link=False` for a deleted maker — its page is gone."""
    return templates.TemplateResponse(
        request=request,
        name="_review_done.html",
        context={
            "maker_id": maker_id,
            "name": name,
            "action": action,
            "to_go": to_go,
            "link": link,
        },
    )


def _review_blocked(request: Request, maker_id: int, message: str) -> HTMLResponse:
    """HTMX answer to a refused card action (rename collision, out-of-vocabulary
    value, promote collision, ...): the reason, retargeted into the card's
    message slot so the card — and any unsaved edits in its form — stays put.
    Status 200 on purpose: htmx doesn't swap 4xx responses by default, and here
    the swapped-in message is the answer. Plain form posts still get the 4xx.
    """
    response = templates.TemplateResponse(
        request=request,
        name="_review_message.html",
        context={"message": message},
    )
    response.headers["HX-Retarget"] = f"#review-msg-{maker_id}"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@router.post("/review/maker/{maker_id}")
def review_save(
    request: Request,
    maker_id: int,
    aesthetic_tags: list[str] = Form(default=[]),
    complication_focus: list[str] = Form(default=[]),
    maker_type: str = Form(default=""),
    price_band: str = Form(default=""),
    output_scale: str = Form(default=""),
    distribution: str = Form(default=""),
    movement_origin: str = Form(default=""),
    country: str = Form(default=""),
    founded_year: str = Form(default=""),
    website: str = Form(default=""),
    is_independent: str = Form(default=""),
    is_pre_launch: str = Form(default=""),
    canonical_name: str = Form(default=""),
    keep_old_name: str = Form(default=""),
) -> Response:
    """Persist edits from the review grid for one maker.

    Human edits are authoritative, so this overwrites (not never-overwrite):
    an emptied field is cleared. Validates taste values against the controlled
    vocabulary first. On save, the edited fields drop their "auto" markers and
    taste_enriched_at is stamped so the auto-tagger won't revisit. Redirects
    back to /review (POST-redirect-GET).

    A changed `canonical_name` renames the maker via rename_maker before any
    other write, keeping the old name as an alias when `keep_old_name` is
    ticked. A blocker (name taken by another maker, seed maker) is a 409 with
    nothing saved. A blank name is ignored rather than cleared.

    Saving is also the review sign-off (4N): it stamps `reviewed_at`, so the
    maker leaves the queue and enrichment won't touch it again. From the HTMX
    card (HX-Request), success answers with the collapsed "saved" row and the
    new "to go" count instead of the 303, and a blocker (out-of-vocabulary
    value, rename collision, missing maker) renders inline in the card, with
    nothing saved, instead of a bare error.
    """
    _require_admin()
    hx = _is_htmx(request)

    submitted_multi = {
        "aesthetic_tags": aesthetic_tags,
        "complication_focus": complication_focus,
    }
    submitted_single = {
        "maker_type": maker_type or None,
        "price_band": price_band or None,
        "output_scale": output_scale or None,
        "distribution": distribution or None,
        "movement_origin": movement_origin or None,
    }
    # Defense-in-depth: reject anything out of vocabulary (the UI only offers
    # valid options, but never trust the request).
    to_check: dict = {k: v for k, v in submitted_multi.items() if v}
    to_check.update({k: v for k, v in submitted_single.items() if v})
    try:
        taste.validate_taste_fields(to_check)
    except ValueError as exc:
        if hx:
            return _review_blocked(request, maker_id, str(exc))
        raise HTTPException(status_code=400, detail=str(exc))

    from datetime import datetime, timezone

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).first()
        if m is None:
            if hx:
                return _review_blocked(
                    request, maker_id, "This maker no longer exists — reload the page."
                )
            raise HTTPException(status_code=404)

        new_name = canonical_name.strip()
        if new_name and new_name != m.canonical_name:
            res = rename_maker(
                session,
                str(maker_id),
                new_name,
                keep_old_as_alias=keep_old_name == "on",
                commit=True,
            )
            if res.blockers:
                if hx:
                    return _review_blocked(request, maker_id, " ".join(res.blockers))
                raise HTTPException(status_code=409, detail=" ".join(res.blockers))

        for field, vals in submitted_multi.items():
            setattr(m, field, vals or None)
        for field, val in submitted_single.items():
            setattr(m, field, val)

        m.country = country.strip() or None
        m.website = website.strip() or None
        fy = founded_year.strip()
        m.founded_year = int(fy) if fy.isdigit() else None
        m.is_independent = is_independent == "on"
        m.is_pre_launch = is_pre_launch == "on"

        editable = set(taste.TASTE_FIELDS) | {
            "country", "founded_year", "website", "is_independent", "is_pre_launch",
        }
        m.enriched_fields = [
            f for f in (m.enriched_fields or []) if f not in editable
        ]
        now = datetime.now(timezone.utc)
        m.taste_enriched_at = now
        m.reviewed_at = now
        session.commit()
        if hx:
            return _review_done(
                request, maker_id, m.canonical_name, "Saved", _review_counts(session)[0]
            )

    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/promote/{maker_id}")
def review_promote(request: Request, maker_id: int) -> Response:
    """Promote a discovered maker into the seed YAML from the review grid.

    Calls the same logic as `cli promote`. On success the maker is appended to
    known_makers.yaml and (being seed now) drops off this page. A hard blocker
    (collision) or a fuzzy near-match returns 409 with the reason — the
    near-match case is intentionally routed to the CLI, where `--force` can
    confirm it after you've seen the dry-run. From the HTMX card the success
    is the collapsed row and the blocker renders inline (see review_save).
    """
    _require_admin()
    hx = _is_htmx(request)
    from watch_app.extraction.promote import promote_to_seed

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).first()
        if m is None:
            if hx:
                return _review_blocked(
                    request, maker_id, "This maker no longer exists — reload the page."
                )
            raise HTTPException(status_code=404)
        result = promote_to_seed(session, m, commit=True, force=False)
        if not result.written:
            reasons = result.blockers + result.needs_confirm
            detail = " ".join(reasons) if reasons else "Could not promote."
            if result.needs_confirm:
                detail += " Use `cli promote --force` if it's genuinely distinct."
            if hx:
                return _review_blocked(request, maker_id, detail)
            raise HTTPException(status_code=409, detail=detail)
        if hx:
            return _review_done(
                request,
                maker_id,
                m.canonical_name,
                "Promoted to seed",
                _review_counts(session)[0],
            )

    return RedirectResponse(url="/review", status_code=303)


# --- Maker maintenance from the review grid (admin-only) ---
#
# The same suppress / delete operations 4D put on the detail page, surfaced on
# /review so a discovered maker can be curated without opening its page. These
# mirror the detail-page routes but stay on /review: suppress redirects back
# here (the maker drops off the grid, which already filters suppressed makers),
# and delete is routed through an in-card confirm panel (GET
# /review?confirm=delete&maker=…, a dry-run) before this POST commits and
# redirects back here. The dry-run/commit semantics and the seed guard are
# inherited from maker_admin; these differ from the detail-page routes only in
# where they redirect. Since 4N the card drives them over HTMX: the confirm
# panel is fetched into the card (GET /review/confirm-delete/{id}), and each
# POST answers an HX-Request with the collapsed row (or an inline blocker)
# instead of the redirect, so the grid never reloads.


@router.get("/review/confirm-delete/{maker_id}", response_class=HTMLResponse)
def review_confirm_delete(request: Request, maker_id: int) -> HTMLResponse:
    """HTMX: one card's delete confirm panel — a delete dry-run, the same panel
    GET /review?confirm=delete&maker=… renders in the full page — swapped into
    the card without reloading the grid. Its Cancel just clears the panel."""
    _require_admin()
    with session_scope() as session:
        preview = delete_maker(session, str(maker_id), commit=False)
    return templates.TemplateResponse(
        request=request,
        name="_review_delete_panel.html",
        context={"delete_preview": preview, "maker_id": maker_id, "inline": True},
    )


@router.post("/review/suppress/{maker_id}")
def review_suppress(request: Request, maker_id: int) -> Response:
    """Suppress a discovered maker from the review grid. Redirects to /review;
    the maker drops off the grid since suppressed makers are filtered there.
    Reverse it from the maker's detail page (it's no longer shown here)."""
    _require_admin()
    hx = _is_htmx(request)
    with session_scope() as session:
        res = suppress_maker(session, str(maker_id), commit=True)
        if res.blockers:
            if hx:
                return _review_blocked(request, maker_id, " ".join(res.blockers))
            raise HTTPException(status_code=404, detail=" ".join(res.blockers))
        if hx:
            return _review_done(
                request, maker_id, res.maker_name, "Suppressed", _review_counts(session)[0]
            )
    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/delete/{maker_id}")
def review_delete(request: Request, maker_id: int) -> Response:
    """Hard-delete a maker from the review grid. The in-card confirm panel
    (GET /review?confirm=delete&maker=…) shows the dry-run impact first. Seed
    makers are blocked and surface as 409, same as the detail-page route."""
    _require_admin()
    hx = _is_htmx(request)
    with session_scope() as session:
        plan = delete_maker(session, str(maker_id), commit=True)
        if plan.blockers:
            if hx:
                return _review_blocked(request, maker_id, " ".join(plan.blockers))
            raise HTTPException(status_code=409, detail=" ".join(plan.blockers))
        if hx:
            return _review_done(
                request,
                maker_id,
                plan.maker_name,
                "Deleted",
                _review_counts(session)[0],
                link=False,
            )
    return RedirectResponse(url="/review", status_code=303)


# --- Bulk-suppress the audit's "not a maker" verdicts (admin-only, chunk 4P) ---
#
# The web audit (4O) sorts its "no" verdicts to the top of the queue, but each
# card still took its own Suppress. This page lists them all at once, from
# the header's "Not a maker (N)" link: the ones with no brand suggestion
# ticked, the ones with a suggestion unticked — a founder's card is usually
# better merged into its brand from the card itself, which makes the name an
# alias. One post suppresses whatever is ticked, recording the audit's
# reasoning as the suppression reason; undo stays per maker (Unsuppress on its
# page, or `cli unsuppress`).


@router.get("/review/bulk-suppress", response_class=HTMLResponse)
def review_bulk_suppress_page(request: Request) -> HTMLResponse:
    """The checklist: every queue maker the audit judged "no", in two groups —
    no brand suggested (ticked) and brand suggested (unticked) — each with
    the audit's reason and sources. A suggestion that resolves to the maker
    itself shows no brand line on its card, so it counts as none here too."""
    _require_admin()
    with session_scope() as session:
        makers = _not_a_maker_queue(session)
        targets = _brand_targets(session) if any(m.audit_brand for m in makers) else {}
        rows = [
            {
                "id": m.id,
                "name": m.canonical_name,
                "reason": m.audit_reason,
                "sources": _audit_source_links(m),
                "brand": _brand_suggestion(m, targets),
            }
            for m in makers
        ]
        to_go = _review_counts(session)[0]
    return templates.TemplateResponse(
        request=request,
        name="review_bulk_suppress.html",
        context={
            "no_brand": [r for r in rows if r["brand"] is None],
            "with_brand": [r for r in rows if r["brand"] is not None],
            "to_go": to_go,
        },
    )


@router.post("/review/bulk-suppress")
def review_bulk_suppress(maker_id: list[int] = Form(default=[])) -> RedirectResponse:
    """Suppress the ticked makers, recording "Audit: <its reason>" as each one's
    suppression reason. Every id is checked again first: only makers still in
    the queue with a "no" verdict are suppressed, so anything reviewed,
    suppressed, re-audited or deleted since the page loaded — or a seed maker,
    or an id that isn't a maker — is skipped and counted instead. Redirects to
    /review, which shows both counts."""
    _require_admin()
    ticked = list(dict.fromkeys(maker_id))
    suppressed = 0
    with session_scope() as session:
        eligible = {m.id: m for m in _not_a_maker_queue(session)}
        for mid in ticked:
            m = eligible.get(mid)
            if m is None:
                continue
            reason = f"Audit: {m.audit_reason or 'not a maker'}"[:300]
            if suppress_maker(session, str(mid), reason=reason, commit=True).done:
                suppressed += 1
    skipped = len(ticked) - suppressed
    return RedirectResponse(
        url=f"/review?suppressed={suppressed}&skipped={skipped}", status_code=303
    )


# --- Taste explorer (Layer 2a, chunk 3J) ---


def _maker_has_taste(m: Maker) -> bool:
    """True if the maker carries at least one taste dimension value."""
    return any(getattr(m, f) for f in taste.TASTE_FIELDS)


def _maker_matches_filters(m: Maker, filters: dict[str, set]) -> bool:
    """Faceted match: OR within a dimension, AND across dimensions.

    A multi-value dimension matches if the maker's list overlaps the selection;
    a single-value dimension matches if its value is among the selected.
    Dimensions with no selection are ignored.
    """
    for dim, selected in filters.items():
        if not selected:
            continue
        value = getattr(m, dim)
        if dim in taste.MULTI_VALUE_FIELDS:
            if not value or not (set(value) & selected):
                return False
        else:
            if value not in selected:
                return False
    return True


def _facet_counts(tagged: list[Maker]) -> dict[str, dict[str, int]]:
    """How many tagged makers carry each vocabulary value (static, per option).

    Computed over the whole tagged universe — not the current selection — so
    the numbers are stable while filtering (the "simple" counts from the
    design). Lets you avoid dead-end selections.
    """
    counts = {f: {v: 0 for v in taste.VOCABULARY[f]} for f in taste.TASTE_FIELDS}
    for m in tagged:
        for f in taste.MULTI_VALUE_FIELDS:
            for v in getattr(m, f) or []:
                if v in counts[f]:
                    counts[f][v] += 1
        for f in taste.SINGLE_VALUE_FIELDS:
            v = getattr(m, f)
            if v in counts[f]:
                counts[f][v] += 1
    return counts


_VALID_WINDOWS = (3, 7, 14, 30)
_VALID_WINDOW_BY = ("activity", "discovered")


def _parse_window(window: str, window_by: str) -> tuple[int | None, str]:
    """Normalize the /explore window controls.

    Returns (window_days, window_by): window_days is None for all-time (no
    filter) or an int in _VALID_WINDOWS; window_by falls back to "activity".
    """
    by = window_by if window_by in _VALID_WINDOW_BY else "activity"
    try:
        days = int(window)
    except (TypeError, ValueError):
        return None, by
    return (days if days in _VALID_WINDOWS else None), by


def _window_note(window_days: int | None, window_by: str) -> str:
    """Human caption for the active window, or "" for all-time."""
    if not window_days:
        return ""
    basis = "mentioned" if window_by == "activity" else "newly discovered"
    return f"last {window_days} days · {basis}"


def _maker_ids_in_window(
    session, window_days: int | None, window_by: str
) -> set[int] | None:
    """Maker ids inside the window, or None for all-time (no filtering).

    - "discovered": Maker.first_seen_at >= cutoff
    - "activity":   has a Mention whose effective date — published_at, else
      discovered_at — is >= cutoff

    The comparison runs in SQL (the same approach fetch_rank_changes uses) so
    SQLite handles the stored datetimes; comparing in Python would mix
    naive/aware values and raise.
    """
    from datetime import datetime, timedelta, timezone

    if not window_days:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    if window_by == "discovered":
        rows = session.query(Maker.id).filter(Maker.first_seen_at >= cutoff).all()
    else:  # activity
        effective = func.coalesce(Mention.published_at, Mention.discovered_at)
        rows = (
            session.query(Mention.maker_id)
            .filter(effective >= cutoff)
            .distinct()
            .all()
        )
    return {r[0] for r in rows}


def _explore(
    preset: str,
    filters: dict[str, set],
    window_days: int | None = None,
    window_by: str = "activity",
    state: str | None = None,
) -> dict:
    """Filter the tagged-maker universe and rank the matches by preset score.

    Returns the result rows (re-ranked 1..N within the filtered set), the
    static facet counts, and the size of the tagged universe.

    `state` (one of RELATIONSHIP_STATES) narrows the universe to makers in that
    personal relationship state — applied alongside the window, before facet
    counts, so counts and total reflect the narrowed set.
    """
    with session_scope() as session:
        tagged = [m for m in session.query(Maker).all() if _maker_has_taste(m) and not m.is_suppressed]
        window_ids = _maker_ids_in_window(session, window_days, window_by)
        if window_ids is not None:
            tagged = [m for m in tagged if m.id in window_ids]
        if state in RELATIONSHIP_STATES:
            tagged = [m for m in tagged if m.relationship_state == state]
        facet = _facet_counts(tagged)
        total_tagged = len(tagged)
        matched = [m for m in tagged if _maker_matches_filters(m, filters)]

        # Rank by the preset's latest scores. Pull the whole ranking once and
        # index it; unscored makers sort to the end.
        ranking = fetch_top_makers(session, preset, limit=100_000)
        order = {r["maker_id"]: i for i, r in enumerate(ranking)}
        score_mc = {r["maker_id"]: (r["score"], r["mention_count"]) for r in ranking}
        matched.sort(key=lambda m: order.get(m.id, 10**9))

        rows = []
        for i, m in enumerate(matched, start=1):
            score, mc = score_mc.get(m.id, (0.0, 0))
            rows.append(
                {
                    "maker_id": m.id,
                    "rank": i,
                    "name": m.canonical_name,
                    "country": m.country,
                    "score": score,
                    "mention_count": mc,
                    "relationship_state": m.relationship_state,
                }
            )
    return {"makers": rows, "facet_counts": facet, "total_tagged": total_tagged}


_TASTE_FILTER_PARAMS = list(taste.TASTE_FIELDS)


def _resolve_preset(preset: str | None) -> str:
    return preset if preset in PRESETS else get_settings().default_preset


@router.get("/explore", response_class=HTMLResponse)
def explore(
    request: Request,
    preset: str = Query(None),
    window: str = Query("all"),
    window_by: str = Query("activity"),
    state: str = Query("all"),
) -> HTMLResponse:
    """The taste explorer: filter makers by taste dimensions, rank by preset.

    Operates on every maker carrying at least one taste tag (seed and
    discovered alike). Faceted filtering — OR within a dimension, AND across —
    with static per-option counts; results ranked by the selected preset's
    score. Untagged makers are absent (nothing to filter them on).
    """
    chosen = _resolve_preset(preset)
    window_days, window_by = _parse_window(window, window_by)
    current_state = state if state in RELATIONSHIP_STATES else None
    data = _explore(
        chosen, filters={}, window_days=window_days, window_by=window_by,
        state=current_state,
    )
    return templates.TemplateResponse(
        request=request,
        name="explore.html",
        context={
            "presets": list(PRESETS.keys()),
            "current_preset": chosen,
            "taste_fields": list(taste.TASTE_FIELDS),
            "vocabulary": taste.VOCABULARY,
            "field_labels": taste.FIELD_LABELS,
            "display_label": taste.display_label,
            "facet_counts": data["facet_counts"],
            "makers": data["makers"],
            "result_count": len(data["makers"]),
            "total_tagged": data["total_tagged"],
            "any_filters": False,
            "valid_windows": list(_VALID_WINDOWS),
            "current_window": str(window_days) if window_days else "all",
            "current_window_by": window_by,
            "window_note": _window_note(window_days, window_by),
            "relationship_states": list(RELATIONSHIP_STATES),
            "state_labels": RELATIONSHIP_STATE_LABELS,
            "current_state": current_state or "all",
        },
    )


@router.get("/explore/results", response_class=HTMLResponse)
def explore_results(
    request: Request,
    preset: str = Query(None),
    window: str = Query("all"),
    window_by: str = Query("activity"),
    state: str = Query("all"),
    aesthetic_tags: list[str] = Query(default=[]),
    complication_focus: list[str] = Query(default=[]),
    maker_type: list[str] = Query(default=[]),
    price_band: list[str] = Query(default=[]),
    output_scale: list[str] = Query(default=[]),
    distribution: list[str] = Query(default=[]),
    movement_origin: list[str] = Query(default=[]),
) -> HTMLResponse:
    """HTMX fragment: the filtered, ranked results for the explorer."""
    chosen = _resolve_preset(preset)
    window_days, window_by = _parse_window(window, window_by)
    current_state = state if state in RELATIONSHIP_STATES else None
    raw = {
        "aesthetic_tags": aesthetic_tags,
        "complication_focus": complication_focus,
        "maker_type": maker_type,
        "price_band": price_band,
        "output_scale": output_scale,
        "distribution": distribution,
        "movement_origin": movement_origin,
    }
    filters = {dim: set(vals) for dim, vals in raw.items() if vals}
    data = _explore(
        chosen, filters, window_days=window_days, window_by=window_by,
        state=current_state,
    )
    return templates.TemplateResponse(
        request=request,
        name="_explore_results.html",
        context={
            "makers": data["makers"],
            "result_count": len(data["makers"]),
            "total_tagged": data["total_tagged"],
            "any_filters": bool(filters) or current_state is not None,
            "window_note": _window_note(window_days, window_by),
        },
    )


# --- "Makers like this" similarity (Layer 2b, chunk 3K) ---


def _maker_taste_dict(m: Maker) -> dict:
    return {f: getattr(m, f) for f in taste.TASTE_FIELDS}


def _shared_labels(a: dict, b: dict, cap: int = 4) -> list[str]:
    """Human labels for the taste values two makers share — the match's "why".

    Vocabulary order is preserved for determinism.
    """
    labels: list[str] = []
    for field in taste.TASTE_FIELDS:
        av, bv = a.get(field), b.get(field)
        if not av or not bv:
            continue
        if field in taste.MULTI_VALUE_FIELDS:
            bset = set(bv)
            for v in av:
                if v in bset:
                    labels.append(taste.display_label(v))
        elif av == bv:
            labels.append(taste.display_label(av))
    return labels[:cap]


def _similar_makers(maker_id: int, limit: int = 6) -> list[dict]:
    """Top makers most similar to this one by taste (weighted overlap).

    Empty if the target has no taste tags. Makers with no shared populated
    dimension, or zero overlap on the ones they share, are excluded.
    """
    with session_scope() as session:
        target = session.query(Maker).filter_by(id=maker_id).first()
        if target is None or not _maker_has_taste(target):
            return []
        target_taste = _maker_taste_dict(target)

        scored: list[tuple[float, Maker]] = []
        for other in session.query(Maker).all():
            if other.id == maker_id or other.is_suppressed or not _maker_has_taste(other):
                continue
            sim = taste.similarity(target_taste, _maker_taste_dict(other))
            if sim is None or sim <= 0:
                continue
            scored.append((sim, other))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            {
                "maker_id": o.id,
                "name": o.canonical_name,
                "country": o.country,
                "match": round(sim * 100),
                "shared": _shared_labels(target_taste, _maker_taste_dict(o)),
            }
            for sim, o in scored[:limit]
        ]
