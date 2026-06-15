"""Makers router.

Serves:
- GET /                    → full dashboard page
- GET /makers/top          → HTMX fragment, ranked list (swappable by preset)
- GET /makers/{maker_id}   → maker detail page
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from watch_app.config import get_settings
from watch_app import taste
from watch_app.extraction.maker_admin import (
    RELATIONSHIP_STATES,
    RELATIONSHIP_STATE_LABELS,
    TASTE_REACTIONS,
    TASTE_REACTION_LABELS,
    delete_maker,
    find_near_duplicates,
    merge_makers,
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
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.queries import fetch_top_makers, fetch_all_makers_comparison, fetch_rank_changes, latest_scores_query
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Signal


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


# --- Review / edit (admin-only, chunk 3H) ---


def _require_admin() -> None:
    """Gate admin-only routes. 404 (not 403) so the page is invisible when off."""
    if not get_settings().admin_mode:
        raise HTTPException(status_code=404)


def _fetch_discovered_for_review() -> list[dict]:
    """Discovered (non-seed) makers with their taste + factual fields, for the
    review grid. Newest first. Each dict carries the current taste values and
    a per-field `auto` flag so the template can show what still needs vetting.
    """
    from watch_app.scoring.discovery import _load_seed_names

    seed_names = _load_seed_names()
    tracked = taste.TASTE_FIELDS + ("country", "founded_year", "website", "is_independent")
    out: list[dict] = []
    with session_scope() as session:
        rows = session.query(Maker).filter(Maker.is_suppressed.is_(False)).order_by(Maker.first_seen_at.desc()).all()
        for m in rows:
            if m.canonical_name in seed_names:
                continue
            taste_values = {f: getattr(m, f) for f in taste.TASTE_FIELDS}
            out.append(
                {
                    "id": m.id,
                    "canonical_name": m.canonical_name,
                    "country": m.country,
                    "founded_year": m.founded_year,
                    "website": m.website,
                    "is_independent": m.is_independent,
                    "taste": taste_values,
                    "has_taste": any(taste_values.values()),
                    "auto": {f: (f in (m.enriched_fields or [])) for f in tracked},
                }
            )
    return out


@router.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    """Admin page for reviewing and editing taste tags on discovered makers.

    Lists every auto-discovered (non-seed) maker with its seven taste
    dimensions plus the factual fields, all editable. Fields filled by LLM
    enrichment are flagged "auto"; saving an edit clears that flag, since the
    value is now human-reviewed. Seed makers are intentionally absent — their
    taste is curated in the YAML. Gated by WATCH_APP_ADMIN.
    """
    _require_admin()
    makers = _fetch_discovered_for_review()
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "makers": makers,
            "count": len(makers),
            "vocabulary": taste.VOCABULARY,
            "field_labels": taste.FIELD_LABELS,
            "multi_fields": list(taste.MULTI_VALUE_FIELDS),
            "single_fields": list(taste.SINGLE_VALUE_FIELDS),
            "display_label": taste.display_label,
        },
    )


@router.post("/review/maker/{maker_id}")
def review_save(
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
) -> RedirectResponse:
    """Persist edits from the review grid for one maker.

    Human edits are authoritative, so this overwrites (not never-overwrite):
    an emptied field is cleared. Validates taste values against the controlled
    vocabulary first. On save, the edited fields drop their "auto" markers and
    taste_enriched_at is stamped so the auto-tagger won't revisit. Redirects
    back to /review (POST-redirect-GET).
    """
    _require_admin()

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
        raise HTTPException(status_code=400, detail=str(exc))

    from datetime import datetime, timezone

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).first()
        if m is None:
            raise HTTPException(status_code=404)

        for field, vals in submitted_multi.items():
            setattr(m, field, vals or None)
        for field, val in submitted_single.items():
            setattr(m, field, val)

        m.country = country.strip() or None
        m.website = website.strip() or None
        fy = founded_year.strip()
        m.founded_year = int(fy) if fy.isdigit() else None
        m.is_independent = is_independent == "on"

        editable = set(taste.TASTE_FIELDS) | {
            "country", "founded_year", "website", "is_independent",
        }
        m.enriched_fields = [
            f for f in (m.enriched_fields or []) if f not in editable
        ]
        m.taste_enriched_at = datetime.now(timezone.utc)
        session.commit()

    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/promote/{maker_id}")
def review_promote(maker_id: int) -> RedirectResponse:
    """Promote a discovered maker into the seed YAML from the review grid.

    Calls the same logic as `cli promote`. On success the maker is appended to
    known_makers.yaml and (being seed now) drops off this page. A hard blocker
    (collision) or a fuzzy near-match returns 409 with the reason — the
    near-match case is intentionally routed to the CLI, where `--force` can
    confirm it after you've seen the dry-run.
    """
    _require_admin()
    from watch_app.extraction.promote import promote_to_seed

    with session_scope() as session:
        m = session.query(Maker).filter_by(id=maker_id).first()
        if m is None:
            raise HTTPException(status_code=404)
        result = promote_to_seed(session, m, commit=True, force=False)
        if not result.written:
            reasons = result.blockers + result.needs_confirm
            detail = " ".join(reasons) if reasons else "Could not promote."
            if result.needs_confirm:
                detail += " Use `cli promote --force` if it's genuinely distinct."
            raise HTTPException(status_code=409, detail=detail)

    return RedirectResponse(url="/review", status_code=303)


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
