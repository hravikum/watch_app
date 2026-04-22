"""Makers router.

Serves:
- GET /                    → full dashboard page
- GET /makers/top          → HTMX fragment, ranked list (swappable by preset)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from watch_app.config import get_settings
from watch_app.scoring.presets import PRESETS
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Score


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


router = APIRouter()


def _fetch_top(preset_name: str, limit: int = 20) -> list[dict]:
    """Return the latest-scored top makers under a preset.

    Returns plain dicts (not ORM objects) so the template doesn't keep a
    session open.
    """
    with session_scope() as session:
        subq = (
            select(Score.maker_id, Score.value, Score.rank, Score.computed_at)
            .where(Score.preset == preset_name)
            .order_by(Score.computed_at.desc())
        ).subquery()

        rows = (
            session.query(Maker, subq.c.value, subq.c.rank)
            .join(subq, subq.c.maker_id == Maker.id)
            .order_by(subq.c.rank.asc())
            .limit(limit)
            .all()
        )

        return [
            {
                "rank": rank,
                "name": maker.canonical_name,
                "country": maker.country,
                "score": value,
                "mention_count": len(maker.mentions),
            }
            for maker, value, rank in rows
        ]


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
            "makers": _fetch_top(preset),
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
            "makers": _fetch_top(preset),
            "current_preset": preset,
        },
    )
