"""FastAPI application entry point.

Phase 0: a single dashboard page showing top-ranked makers with a preset
toggle. Phase 2 will add maker detail pages, filters, mention feeds, etc.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from watch_app.api.routers import makers


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


app = FastAPI(
    title="Watch App",
    description="Discover up-and-coming independent watchmakers.",
    version="0.1.0",
)

# Serve static files (CSS, JS, images) under /static/.
app.mount(
    "/static",
    StaticFiles(directory=str(WEB_DIR / "static")),
    name="static",
)

# Routers.
app.include_router(makers.router)
