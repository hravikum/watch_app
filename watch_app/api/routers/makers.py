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
from watch_app.scoring.queries import fetch_top_makers, latest_scores_query
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
