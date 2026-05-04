"""Makers router.

Serves:
- GET /                    → full dashboard page
- GET /makers/top          → HTMX fragment, ranked list (swappable by preset)
- GET /makers/{maker_id}   → maker detail page
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from watch_app.config import get_settings
from watch_app.scoring.methodology import (
    SIGNAL_DESCRIPTIONS,
    build_source_contributions,
    build_worked_example,
)
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.queries import fetch_top_makers, fetch_all_makers_comparison, latest_scores_query
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
        }


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
