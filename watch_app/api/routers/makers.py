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
from watch_app.scoring.methodology import (
    SIGNAL_DESCRIPTIONS,
    build_source_contributions,
    build_worked_example,
)
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.queries import fetch_top_makers, fetch_all_makers_comparison, fetch_rank_changes, latest_scores_query
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Signal


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


router = APIRouter()


# --- Helpers ---


def _fetch_top_for_template(preset_name: str, limit: int = 20) -> list[dict]:
    """Wrap fetch_top_makers with the field name the template expects (`name`).

    fetch_top_makers returns canonical_name; template uses name. Also preserves
    maker_id for row click-throughs.
    """
    with session_scope() as session:
        rows = fetch_top_makers(session, preset_name, limit=limit)
    return [
        {
            "maker_id": r["maker_id"],
            "rank": r["rank"],
            "name": r["canonical_name"],
            "country": r["country"],
            "score": r["score"],
            "mention_count": r["mention_count"],
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
            "first_seen_at": maker.first_seen_at,
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
def dashboard(request: Request) -> HTMLResponse:
    """Render the full dashboard page."""
    settings = get_settings()
    preset = settings.default_preset
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "presets": list(PRESETS.keys()),
            "current_preset": preset,
            "makers": _fetch_top_for_template(preset),
        },
    )


@router.get("/makers/top", response_class=HTMLResponse)
def top_fragment(
    request: Request,
    preset: str = Query(...),
) -> HTMLResponse:
    """HTMX endpoint: returns just the <table> fragment.

    Called when the user clicks a preset toggle; the page swaps this fragment
    into place without a full reload.
    """
    if preset not in PRESETS:
        preset = get_settings().default_preset
    return templates.TemplateResponse(
        request=request,
        name="_makers_table.html",
        context={
            "makers": _fetch_top_for_template(preset),
            "current_preset": preset,
        },
    )


@router.get("/makers/{maker_id}", response_class=HTMLResponse)
def maker_detail(request: Request, maker_id: int) -> HTMLResponse:
    """Render the per-maker detail page."""
    detail = _fetch_maker_detail(maker_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Maker {maker_id} not found")
    return templates.TemplateResponse(
        request=request,
        name="maker_detail.html",
        context={"m": detail},
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
        rows = session.query(Maker).order_by(Maker.first_seen_at.desc()).all()
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


def _explore(preset: str, filters: dict[str, set]) -> dict:
    """Filter the tagged-maker universe and rank the matches by preset score.

    Returns the result rows (re-ranked 1..N within the filtered set), the
    static facet counts, and the size of the tagged universe.
    """
    with session_scope() as session:
        tagged = [m for m in session.query(Maker).all() if _maker_has_taste(m)]
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
                }
            )
    return {"makers": rows, "facet_counts": facet, "total_tagged": total_tagged}


_TASTE_FILTER_PARAMS = list(taste.TASTE_FIELDS)


def _resolve_preset(preset: str | None) -> str:
    return preset if preset in PRESETS else get_settings().default_preset


@router.get("/explore", response_class=HTMLResponse)
def explore(request: Request, preset: str = Query(None)) -> HTMLResponse:
    """The taste explorer: filter makers by taste dimensions, rank by preset.

    Operates on every maker carrying at least one taste tag (seed and
    discovered alike). Faceted filtering — OR within a dimension, AND across —
    with static per-option counts; results ranked by the selected preset's
    score. Untagged makers are absent (nothing to filter them on).
    """
    chosen = _resolve_preset(preset)
    data = _explore(chosen, filters={})
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
        },
    )


@router.get("/explore/results", response_class=HTMLResponse)
def explore_results(
    request: Request,
    preset: str = Query(None),
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
    data = _explore(chosen, filters)
    return templates.TemplateResponse(
        request=request,
        name="_explore_results.html",
        context={
            "makers": data["makers"],
            "result_count": len(data["makers"]),
            "total_tagged": data["total_tagged"],
            "any_filters": bool(filters),
        },
    )
