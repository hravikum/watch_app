"""CLI entry point.

Usage:
    python -m watch_app.cli setup-db
    python -m watch_app.cli scan
    python -m watch_app.cli scan --use-llm
    python -m watch_app.cli top
    python -m watch_app.cli top --preset hidden_gem
    python -m watch_app.cli maker "Simon Brette"
    python -m watch_app.cli serve
"""
from __future__ import annotations

import logging
import subprocess
import sys

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from watch_app.config import get_settings
from watch_app.pipeline.orchestrator import run_scan
from watch_app.scoring.presets import PRESETS
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Watch App — discover up-and-coming independent watchmakers.",
)
console = Console()


@app.callback()
def _setup_logging(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command("setup-db")
def setup_db() -> None:
    """Create the database and apply migrations."""
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[cyan]Applying migrations to[/cyan] {settings.db_url}")
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Migration failed.[/red]")
        raise typer.Exit(result.returncode)
    console.print("[green]Database ready.[/green]")


@app.command()
def scan(
    use_llm: bool = typer.Option(
        False,
        "--use-llm",
        help="Use Claude for entity extraction (costs API credits).",
    ),
) -> None:
    """Run a scan: fetch from sources, extract, resolve, score."""
    console.print(
        f"[cyan]Scanning[/cyan] (use_llm={use_llm})..."
    )
    stats = run_scan(use_llm=use_llm)
    console.print(
        f"[green]Done.[/green] "
        f"sources_run={stats['sources_run']} "
        f"mentions_written={stats['mentions_written']} "
        f"makers_scored={stats['makers_scored']}"
    )


@app.command()
def top(
    preset: str = typer.Option(
        None,
        "--preset",
        help=f"One of: {', '.join(PRESETS.keys())}. Defaults to the configured preset.",
    ),
    limit: int = typer.Option(20, "--limit", help="How many makers to show."),
) -> None:
    """Print the top-ranked makers under a preset."""
    settings = get_settings()
    preset_name = preset or settings.default_preset
    if preset_name not in PRESETS:
        console.print(
            f"[red]Unknown preset {preset_name!r}.[/red] "
            f"Known: {', '.join(PRESETS.keys())}"
        )
        raise typer.Exit(1)

    with session_scope() as session:
        # Latest score per maker under this preset.
        # SQLite supports correlated subqueries; this is the portable way.
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

        if not rows:
            console.print(
                "[yellow]No scores yet. Run `make scan` first.[/yellow]"
            )
            return

        table = Table(title=f"Top makers — preset: {preset_name}")
        table.add_column("#", style="dim", width=4)
        table.add_column("Maker")
        table.add_column("Score", justify="right")
        table.add_column("Country", style="dim")

        for maker, value, rank in rows:
            table.add_row(
                str(rank),
                maker.canonical_name,
                f"{value:.2f}",
                maker.country or "—",
            )

        console.print(table)


@app.command()
def maker(name: str = typer.Argument(..., help="Canonical name or alias.")) -> None:
    """Show everything we know about one maker."""
    with session_scope() as session:
        found = (
            session.query(Maker)
            .options(joinedload(Maker.mentions).joinedload(Mention.source))
            .filter(Maker.canonical_name == name)
            .one_or_none()
        )
        if not found:
            # Try aliases.
            all_makers = session.query(Maker).all()
            found = next(
                (m for m in all_makers if name in (m.aliases or [])),
                None,
            )
        if not found:
            console.print(f"[yellow]No maker found matching[/yellow] {name!r}")
            raise typer.Exit(1)

        console.print(f"[bold cyan]{found.canonical_name}[/bold cyan]")
        if found.aliases:
            console.print(f"  aliases: {', '.join(found.aliases)}")
        console.print(f"  country: {found.country or '—'}")
        console.print(f"  founded: {found.founded_year or '—'}")
        console.print(f"  website: {found.website or '—'}")
        console.print(f"  mentions: {len(found.mentions)}")
        console.print()

        for m in sorted(
            found.mentions,
            key=lambda x: x.published_at or x.discovered_at,
            reverse=True,
        ):
            date = (m.published_at or m.discovered_at).strftime("%Y-%m-%d")
            console.print(f"  [dim]{date}[/dim] [{m.source.name}] {m.title or m.url}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Start the FastAPI + HTMX web UI."""
    import uvicorn

    uvicorn.run(
        "watch_app.api.main:app",
        host=host,
        port=port,
        reload=True,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
