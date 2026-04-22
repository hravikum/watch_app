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

from watch_app.config import get_settings
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.queries import fetch_top_makers
from watch_app.storage.db import session_scope


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


router = APIRouter()


def _fetch_top_for_template(preset_name: str, limit: int = 20) -> list[dict]:
    """Wrap fetch_top_makers with the field name the template expects (`name`)."""
    with session_scope() as session:
        rows = fetch_top_makers(session, preset_name, limit=limit)
    # Template uses `name`; our shared helper returns `canonical_name`.
    return [
        {
            "rank": r["rank"],
            "name": r["canonical_name"],
            "country": r["country"],
            "score": r["score"],
            "mention_count": r["mention_count"],
        }
        for r in rows
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
