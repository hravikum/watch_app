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
from sqlalchemy.orm import joinedload

from watch_app.config import get_settings
from watch_app.pipeline.orchestrator import run_scan
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.queries import fetch_top_makers
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention


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
    normalize: bool = typer.Option(
        True,
        "--normalize/--no-normalize",
        help=(
            "Z-score normalize signals before composing scores. "
            "Default on. Disable to see raw-weight behavior."
        ),
    ),
    trigger: str = typer.Option(
        "manual",
        "--trigger",
        help=(
            "Recorded in the scan history to distinguish invocation source. "
            "Use 'scheduled' from a launchd/cron job, 'manual' from the CLI."
        ),
    ),
) -> None:
    """Run a scan: fetch from sources, extract, resolve, score."""
    console.print(
        f"[cyan]Scanning[/cyan] "
        f"(use_llm={use_llm}, normalize={normalize}, trigger={trigger})..."
    )
    stats = run_scan(use_llm=use_llm, normalize=normalize, trigger=trigger)
    console.print(
        f"[green]Done.[/green] "
        f"run_id={stats['scan_run_id']} "
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
        rows = fetch_top_makers(session, preset_name, limit=limit)

        if not rows:
            console.print(
                "[yellow]No scores yet. Run `make scan` first.[/yellow]"
            )
            return

        table = Table(title=f"Top makers — preset: {preset_name}")
        table.add_column("#", style="dim", width=4)
        table.add_column("Maker")
        table.add_column("Mentions", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Country", style="dim")

        for row in rows:
            table.add_row(
                str(row["rank"]),
                row["canonical_name"],
                str(row["mention_count"]),
                f"{row['score']:.2f}",
                row["country"] or "—",
            )

        console.print(table)


@app.command()
def digest(
    preset: str = typer.Option(
        None,
        "--preset",
        help=f"One of: {', '.join(PRESETS.keys())}. Defaults to the configured preset.",
    ),
    days: int = typer.Option(7, "--days", help="Window length in days (1-30)."),
    markdown: bool = typer.Option(
        False, "--markdown", help="Emit a portable markdown document instead of a table."
    ),
) -> None:
    """Summarize what changed in the last `days` days: new discoveries,

    movers, and activity on the makers you've marked watching/target.
    """
    from watch_app.scoring.digest import (
        compute_weekly_digest,
        render_digest_markdown,
        _fmt_change,
    )

    preset_name = preset or get_settings().default_preset
    if preset_name not in PRESETS:
        console.print(
            f"[red]Unknown preset {preset_name!r}.[/red] Known: {', '.join(PRESETS.keys())}"
        )
        raise typer.Exit(1)

    with session_scope() as session:
        d = compute_weekly_digest(session, window_days=days, preset=preset_name)

        if markdown:
            # Plain stdout so it pipes/redirects cleanly to a file.
            print(render_digest_markdown(d))
            return

        console.print(
            f"[bold cyan]What changed[/bold cyan] — last {d.window_days} days "
            f"[dim](preset: {d.preset})[/dim]"
        )
        if not d.has_history:
            console.print(
                "[yellow]No score history older than this window yet — rank "
                "movement is provisional.[/yellow]"
            )
        if d.is_empty:
            console.print("[dim]Nothing surfaced in this window.[/dim]")
            return

        console.print(f"\n[bold]New discoveries[/bold] ({len(d.new_discoveries)})")
        if d.new_discoveries:
            for m in d.new_discoveries:
                country = f" [dim]· {m.country}[/dim]" if m.country else ""
                console.print(
                    f"  • {m.canonical_name}{country} "
                    f"[dim]({m.mention_count} mention(s))[/dim]"
                )
        else:
            console.print("  [dim]none[/dim]")

        console.print(f"\n[bold]On your list[/bold] ({len(d.watching)})")
        if d.watching:
            wt = Table(show_header=True, header_style="dim")
            wt.add_column("Maker")
            wt.add_column("State", style="dim")
            wt.add_column("New mentions", justify="right")
            wt.add_column("Rank", justify="right")
            for a in d.watching:
                rank_cell = (
                    _fmt_change(a.rank_change)
                    if a.rank_change
                    else (f"#{a.current_rank}" if a.current_rank is not None else "—")
                )
                label = a.relationship_state + (" · new" if a.newly_discovered else "")
                wt.add_row(a.canonical_name, label, str(a.new_mentions), rank_cell)
            console.print(wt)
        else:
            console.print("  [dim]no movement on watched or target makers[/dim]")

        console.print(f"\n[bold]Movers[/bold] — {d.preset}")
        _print_mover_block("Climbers", d.climbers)
        _print_mover_block("Fallers", d.fallers)
        _print_mover_block("New entrants", d.new_entrants, entrants=True)


def _print_mover_block(title: str, rows: list, entrants: bool = False) -> None:
    from watch_app.scoring.digest import _fmt_change

    console.print(f"  [bold dim]{title}[/bold dim] ({len(rows)})")
    if not rows:
        console.print("    [dim]none[/dim]")
        return
    for r in rows:
        if entrants:
            console.print(
                f"    • {r['canonical_name']} [dim]entered at #{r['current_rank']}[/dim]"
            )
        else:
            console.print(
                f"    • {r['canonical_name']} "
                f"[dim]#{r['prior_rank']} → #{r['current_rank']} "
                f"({_fmt_change(r['rank_change'])})[/dim]"
            )


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


@app.command()
def history(
    limit: int = typer.Option(20, "--limit", help="Max runs to show."),
) -> None:
    """Show recent scan run history."""
    from watch_app.storage.models import ScanRun

    with session_scope() as session:
        runs = (
            session.query(ScanRun)
            .order_by(ScanRun.started_at.desc())
            .limit(limit)
            .all()
        )

        if not runs:
            console.print("[yellow]No scan runs recorded yet.[/yellow]")
            return

        table = Table(title=f"Last {len(runs)} scan runs")
        table.add_column("ID", style="dim", width=6)
        table.add_column("Started", width=19)
        table.add_column("Duration", justify="right", width=10)
        table.add_column("Status", width=10)
        table.add_column("Trigger", style="dim", width=10)
        table.add_column("Mentions", justify="right", width=9)
        table.add_column("Makers", justify="right", width=8)

        for r in runs:
            if r.finished_at:
                dur = r.duration_seconds
                dur_str = f"{dur:.1f}s" if dur is not None else "—"
            else:
                dur_str = "(running)"
            status_color = {
                "completed": "green",
                "failed": "red",
                "running": "yellow",
            }.get(r.status, "white")
            table.add_row(
                str(r.id),
                r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                dur_str,
                f"[{status_color}]{r.status}[/{status_color}]",
                r.trigger,
                str(r.mentions_written) if r.mentions_written is not None else "—",
                str(r.makers_scored) if r.makers_scored is not None else "—",
            )

        console.print(table)

        # If any recent runs failed, show the latest error.
        failed = [r for r in runs if r.status == "failed" and r.error_message]
        if failed:
            console.print()
            console.print(
                f"[red]Most recent failure (run {failed[0].id}):[/red] "
                f"{failed[0].error_message}"
            )


@app.command()
def enrich(
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Actually write changes to the DB. Without this flag, runs in "
             "dry-run mode (shows proposals only).",
    ),
    taste: bool = typer.Option(
        False,
        "--taste",
        help="Tag auto-discovered independents with taste / character "
             "dimensions instead of the factual fields. Dry-run unless "
             "--commit is given.",
    ),
    retag: bool = typer.Option(
        False,
        "--retag",
        help="With --taste: also re-attempt makers that were tried before but "
             "got no tags (e.g. earlier abstentions), now that the prompt uses "
             "press-mention context. Skips makers that already have tags. "
             "No effect without --taste.",
    ),
) -> None:
    """Enrich auto-discovered makers with missing metadata via LLM lookup.

    For each Maker with missing fields (website, country, is_independent),
    asks Claude for the value and (with --commit) writes verified results.

    Per-field verification:
    - website: HTTP-resolve (must return 2xx/3xx), blocked-domain check
    - country: must match a known watchmaking country
    - is_independent: must be YES or NO, not NONE

    Default behavior is dry-run: prints proposals to the terminal without
    writing anything. Re-run with --commit to apply. LLM responses are
    cached, so dry-run + commit doesn't double-bill.

    Note: automatic enrichment also fires after every scan completes (2T),
    so this CLI is primarily useful for backfilling existing rows or for
    seeing what would be proposed before automation runs it.

    Cost: roughly $0.003 per maker enriched (one Haiku call per field).
    Backfilling 30 makers across 3 fields costs ~$0.10 total.

    With --taste, instead tags auto-discovered independents with the seven
    taste / character dimensions (one Haiku call per maker, constrained to the
    controlled vocabulary, abstaining when unsure). Useful for backfilling
    makers discovered before 3F; new discoveries are tagged automatically
    after each scan. The prompt is informed by the maker's recent press
    mentions (3G), which sharply reduces abstention on niche makers.

    Add --retag to also re-attempt makers tried before but left untagged
    (earlier abstentions) — useful right after the context-aware prompt
    landed. It skips makers that already have tags, so it never clobbers.
    """
    from rich.console import Console
    from rich.table import Table

    from watch_app.extraction.enrich import (
        FIELDS,
        apply_enrichments,
        iter_unenriched_makers,
        propose_enrichments,
    )

    console = Console()
    mode = "COMMIT" if commit else "DRY-RUN"

    if taste:
        from watch_app import taste as taste_vocab
        from watch_app.extraction.enrich import (
            apply_taste_proposals,
            iter_untagged_discovered_independents,
            propose_taste_for_discovered,
        )

        console.print(f"[bold]Taste tagging — discovered makers ({mode})[/bold]")
        if retag:
            console.print(
                "[dim]--retag: re-attempting previously-untagged makers with "
                "mention context.[/dim]"
            )
        with session_scope() as session:
            n = len(
                iter_untagged_discovered_independents(
                    session, include_attempted=retag
                )
            )
            if n == 0:
                console.print(
                    "[green]No untagged auto-discovered independents. "
                    "Nothing to do.[/green]"
                )
                return
            console.print(
                f"Found [yellow]{n}[/yellow] untagged discovered "
                f"independent(s). Drafting taste via Claude..."
            )
            proposals = propose_taste_for_discovered(
                session, include_attempted=retag
            )

            table = Table(title="Proposed taste tags")
            table.add_column("Maker", style="cyan")
            table.add_column("Dimension")
            table.add_column("Value(s)")
            tagged = 0
            abstained = 0
            for p in proposals:
                if not p.tags:
                    abstained += 1
                    table.add_row(
                        p.canonical_name, "[dim]—[/dim]", "[dim]abstained[/dim]"
                    )
                    continue
                tagged += 1
                first = True
                for field_name in taste_vocab.TASTE_FIELDS:
                    if field_name not in p.tags:
                        continue
                    value = p.tags[field_name]
                    display = (
                        ", ".join(value) if isinstance(value, list) else str(value)
                    )
                    table.add_row(
                        p.canonical_name if first else "",
                        taste_vocab.FIELD_LABELS.get(field_name, field_name),
                        display,
                    )
                    first = False
            console.print(table)
            console.print(
                f"\n[bold]Summary:[/bold] {tagged} maker(s) with tags, "
                f"{abstained} abstained."
            )

            if not commit:
                console.print(
                    "\n[yellow]Dry run.[/yellow] Re-run with [bold]--commit[/bold] "
                    "to write tags and mark the listed makers as taste-attempted."
                )
                return

            written = apply_taste_proposals(session, proposals)
            console.print(
                f"\n[green]Wrote {written} taste field value(s); marked "
                f"{len(proposals)} maker(s) as taste-attempted.[/green]"
            )
        return

    console.print(f"[bold]Maker enrichment ({mode})[/bold]")

    with session_scope() as session:
        unenriched_count = sum(1 for _ in iter_unenriched_makers(session))
        if unenriched_count == 0:
            console.print("[green]All makers fully enriched. Nothing to do.[/green]")
            return

        console.print(
            f"Found [yellow]{unenriched_count}[/yellow] maker(s) with "
            f"missing field(s). Looking them up via Claude..."
        )

        proposals = propose_enrichments(session)

        table = Table(title="Proposed enrichments")
        table.add_column("Maker", style="cyan")
        table.add_column("Field")
        table.add_column("Proposed value")
        table.add_column("Status", style="bold")

        committable = 0
        skipped = 0
        for p in proposals:
            if not p.field_proposals:
                # Maker had all fields populated; nothing to do.
                continue
            for fp in p.field_proposals:
                if fp.is_committable:
                    status = "[green]✓ ready[/green]"
                    committable += 1
                else:
                    status = f"[dim]skip ({fp.rejection_reason})[/dim]"
                    skipped += 1
                # Format the value for display (booleans → text)
                if fp.proposed_value is None:
                    display = "—"
                elif isinstance(fp.proposed_value, bool):
                    display = "independent" if fp.proposed_value else "conglomerate"
                else:
                    display = str(fp.proposed_value)
                table.add_row(
                    p.canonical_name,
                    fp.field_name,
                    display,
                    status,
                )
        console.print(table)

        console.print(
            f"\n[bold]Summary:[/bold] {committable} field(s) committable, "
            f"{skipped} skipped."
        )

        if not commit:
            console.print(
                "\n[yellow]Dry run.[/yellow] Re-run with [bold]--commit[/bold] "
                "to write the committable proposals to the database."
            )
            return

        written = apply_enrichments(session, proposals)
        console.print(f"\n[green]Wrote {written} field value(s) to the database.[/green]")


@app.command("draft-taste")
def draft_taste(
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Write drafts into known_makers.yaml. Without this flag, runs "
             "in dry-run mode (shows proposals only).",
    ),
    limit: int = typer.Option(
        0, "--limit", help="Only process the first N untagged independents (0 = all)."
    ),
    only: str = typer.Option(
        "",
        "--only",
        help="Comma-separated canonical names to draft, overriding the "
             "untagged-independent scan (used for the pilot).",
    ),
) -> None:
    """Draft taste tags for untagged independent makers via Haiku, into the YAML.

    Curation aid for Layer 1b. For each untagged independent maker, asks
    Claude for the seven taste dimensions constrained to the controlled
    vocabulary, sanitizes the result, and (with --commit) writes drafts into
    known_makers.yaml for review. Conglomerates are skipped.

    Default is dry-run: prints proposals without writing. LLM responses are
    cached, so dry-run + commit doesn't double-bill. Always review the diff
    (git diff watch_app/data/known_makers.yaml) before committing the YAML.

    Cost: one Haiku call per maker (~$0.001 each). All 60 untagged
    independents cost well under $0.10.
    """
    from rich.table import Table

    from watch_app import taste
    from watch_app.extraction.seed import _load_yaml
    from watch_app.extraction.taste_draft import (
        is_tagged,
        propose_taste,
        untagged_independents,
        write_drafts_to_yaml,
    )

    mode = "COMMIT" if commit else "DRY-RUN"
    console.print(f"[bold]Taste drafting ({mode})[/bold]")

    entries = _load_yaml()
    if only.strip():
        wanted = {n.strip() for n in only.split(",") if n.strip()}
        by_name = {e.get("canonical_name"): e for e in entries}
        targets = []
        for n in wanted:
            match = by_name.get(n)
            if match is None:
                console.print(f"[yellow]Not in seed, skipping:[/yellow] {n}")
            elif is_tagged(match):
                console.print(f"[dim]Already tagged, skipping:[/dim] {n}")
            else:
                targets.append(match)
    else:
        targets = untagged_independents(entries)
        if limit > 0:
            targets = targets[:limit]

    if not targets:
        console.print("[green]Nothing to draft.[/green]")
        return

    console.print(f"Drafting [yellow]{len(targets)}[/yellow] maker(s) via Haiku...")

    display_fields = [
        "aesthetic_tags",
        "complication_focus",
        "maker_type",
        "price_band",
        "output_scale",
        "distribution",
        "movement_origin",
    ]

    proposals = []
    with session_scope() as session:
        for entry in targets:
            proposals.append(propose_taste(session, entry))

    table = Table(title="Proposed taste tags")
    table.add_column("Maker", style="cyan")
    for f in display_fields:
        table.add_column(taste.FIELD_LABELS[f])

    filled = 0
    for p in proposals:
        if p.tags:
            filled += 1

        def cell(field: str) -> str:
            value = p.tags.get(field)
            if value is None:
                return "[dim]—[/dim]"
            return ", ".join(value) if isinstance(value, list) else value

        table.add_row(p.canonical_name, *(cell(f) for f in display_fields))

    console.print(table)
    console.print(f"{filled}/{len(proposals)} maker(s) got at least one tag.")

    if commit:
        written = write_drafts_to_yaml(proposals)
        console.print(
            f"\n[green]Wrote drafts for {written} maker(s) into "
            f"known_makers.yaml.[/green]"
        )
        console.print(
            "[dim]Review with: git diff watch_app/data/known_makers.yaml[/dim]"
        )
    else:
        console.print("[dim]Dry-run. Re-run with --commit to write the YAML.[/dim]")


@app.command()
def schedule() -> None:
    """Print instructions for setting up a daily scheduled scan via launchd.

    Uses a LaunchAgent (per-user) which runs when you're logged in and
    catches up on missed runs if your Mac was asleep or off at the
    scheduled time — better than cron for a laptop.
    """
    import os
    from pathlib import Path

    # Figure out the user's python and the project root.
    project_root = Path(__file__).resolve().parent.parent
    venv_python = project_root / "venv" / "bin" / "python"
    launchagents_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = launchagents_dir / "com.user.watchapp.scan.plist"

    plist_contents = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.watchapp.scan</string>

    <key>ProgramArguments</key>
    <array>
        <string>{venv_python}</string>
        <string>-m</string>
        <string>watch_app.cli</string>
        <string>scan</string>
        <string>--trigger</string>
        <string>scheduled</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{project_root}</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>7</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>{project_root}/data/scan.log</string>

    <key>StandardErrorPath</key>
    <string>{project_root}/data/scan.err.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""

    console.print(
        "[bold cyan]Setting up daily scans via launchd[/bold cyan]\n"
    )
    console.print(
        "This will schedule a scan every day at 7:00 AM (local time).\n"
        "launchd catches up if your Mac was asleep at 7am.\n"
    )

    # Check that venv exists; complain if not.
    if not venv_python.exists():
        console.print(
            f"[red]Warning:[/red] expected venv python at {venv_python} "
            f"but it doesn't exist. Create the venv first:\n"
            f"  cd {project_root} && python3 -m venv venv && "
            f"./venv/bin/pip install -r requirements.txt\n"
        )

    console.print(
        f"[bold]Step 1.[/bold] Save the plist below to:\n"
        f"    [green]{plist_path}[/green]\n"
    )
    console.print("--- BEGIN PLIST ---")
    console.print(plist_contents)
    console.print("--- END PLIST ---\n")

    console.print(
        f"[bold]Step 2.[/bold] Load it into launchd:\n"
        f"    [cyan]launchctl unload {plist_path} 2>/dev/null || true[/cyan]\n"
        f"    [cyan]launchctl load {plist_path}[/cyan]\n"
    )
    console.print(
        f"[bold]Step 3.[/bold] Verify it's loaded:\n"
        f"    [cyan]launchctl list | grep watchapp[/cyan]\n"
        f"(should show 'com.user.watchapp.scan' with exit code '-' until first run)\n"
    )
    console.print(
        f"[bold]Step 4.[/bold] Test it runs now (optional):\n"
        f"    [cyan]launchctl start com.user.watchapp.scan[/cyan]\n"
        f"    [cyan]watch-app history[/cyan]  (after a few seconds)\n"
    )
    console.print(
        f"[bold]To disable later:[/bold]\n"
        f"    [cyan]launchctl unload {plist_path}[/cyan]\n"
    )

    # Also offer to write the file automatically.
    console.print(
        f"[dim]Shortcut: to save the plist automatically, run:\n"
        f"    watch-app schedule > /tmp/_wa.txt && "
        f"sed -n '/BEGIN PLIST/,/END PLIST/p' /tmp/_wa.txt | "
        f"sed '1d;$d' > {plist_path}\n"
        f"(The sed pipeline strips the BEGIN/END markers.)[/dim]"
    )


@app.command()
def promote(
    maker: str = typer.Argument(
        ..., help="Maker to promote, by numeric id or exact canonical name."
    ),
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Append the entry to known_makers.yaml. Without this flag, runs "
        "in dry-run mode (shows the entry and any warnings only).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Proceed past a fuzzy near-match warning (use only after "
        "confirming the maker is genuinely distinct).",
    ),
) -> None:
    """Promote an auto-discovered maker into the curated seed YAML.

    Serializes the maker's current curated fields (name, aliases, country,
    founded year, website, is_independent, and its populated taste dimensions)
    into a new entry appended under a "Promoted from discovery" section. On the
    next scan the seed loader reconciles onto the existing DB row, and the maker
    leaves the discovered views.

    Default is dry-run: prints the entry plus any blockers/warnings and writes
    nothing. Re-run with --commit to write, then review the diff
    (git diff watch_app/data/known_makers.yaml) before committing the YAML.

    Tip: tag the maker fully in /review first — promotion is the graduation
    step, and a promoted maker is curated via the YAML from then on.
    """
    from watch_app.extraction.promote import promote_to_seed

    with session_scope() as session:
        target = None
        if maker.isdigit():
            target = session.query(Maker).filter_by(id=int(maker)).first()
        if target is None:
            target = session.query(Maker).filter_by(canonical_name=maker).first()
        if target is None:
            console.print(f"[red]No maker found for '{maker}'.[/red]")
            raise typer.Exit(1)

        mode = "COMMIT" if commit else "DRY-RUN"
        console.print(
            f"[bold]Promote '{target.canonical_name}' \u2192 seed YAML ({mode})[/bold]\n"
        )
        result = promote_to_seed(session, target, commit=commit, force=force)

        console.print("[dim]Entry to append:[/dim]")
        console.print(result.entry_text.rstrip())
        console.print("")
        for blocker in result.blockers:
            console.print(f"[red]\u2717 {blocker}[/red]")
        for item in result.needs_confirm:
            console.print(f"[yellow]\u26a0 {item}[/yellow]")
        for warning in result.warnings:
            console.print(f"[dim]\u2022 {warning}[/dim]")

        if result.written:
            console.print(
                "\n[green]Written.[/green] Review with "
                "[dim]git diff watch_app/data/known_makers.yaml[/dim] and commit."
            )
        elif not commit:
            hint = "Re-run with --commit to write."
            if result.needs_confirm:
                hint += " Add --force to proceed past the near-match."
            console.print(f"\n[yellow]Dry run — nothing written.[/yellow] {hint}")
        elif result.blockers:
            console.print("\n[red]Not written — resolve the blocker above.[/red]")
        elif result.needs_confirm:
            console.print(
                "\n[yellow]Not written — near-match. Re-run with --force if "
                "the maker is genuinely distinct.[/yellow]"
            )


@app.command()
def dupes(
    threshold: int = typer.Option(
        85, "--threshold", help="Minimum WRatio score (0-100) to report."
    ),
) -> None:
    """List near-duplicate makers — pairs with very similar names — for review.

    These are candidates that may be the same maker split across two rows.
    Eyeball them and merge any real duplicates with `watch-app merge`.
    """
    from watch_app.extraction.maker_admin import find_near_duplicates

    with session_scope() as session:
        pairs = find_near_duplicates(session, threshold=threshold)
        if not pairs:
            console.print(f"[green]No maker pairs scored >= {threshold}.[/green]")
            return
        table = Table(title=f"Near-duplicate makers (WRatio >= {threshold})")
        table.add_column("Score", justify="right")
        table.add_column("Maker A")
        table.add_column("id", justify="right")
        table.add_column("Maker B")
        table.add_column("id", justify="right")
        for p in pairs:
            table.add_row(
                f"{p.score:.0f}", p.a_name, str(p.a_id), p.b_name, str(p.b_id)
            )
        console.print(table)
        console.print(
            "[dim]Merge a real duplicate with: "
            "watch-app merge <from-id> <into-id> --commit[/dim]"
        )


@app.command()
def merge(
    from_maker: str = typer.Argument(
        ..., help="Maker to merge FROM (id or name) — must be a discovered maker."
    ),
    into_maker: str = typer.Argument(
        ..., help="Maker to merge INTO (id or name) — the surviving maker."
    ),
    commit: bool = typer.Option(
        False, "--commit", help="Apply the merge. Dry-run otherwise."
    ),
) -> None:
    """Merge one maker into another: reassign its mentions and watch models,

    fold its name + aliases in as aliases, drop its derived rows, and delete it.
    Dry-run by default. The FROM maker must be a discovered (non-seed) maker.
    """
    from watch_app.extraction.maker_admin import merge_makers

    with session_scope() as session:
        plan = merge_makers(session, from_maker, into_maker, commit=commit)
        for blocker in plan.blockers:
            console.print(f"[red]\u2717 {blocker}[/red]")
        if plan.blockers:
            raise typer.Exit(1)

        console.print(f"[bold]Merge '{plan.from_name}' \u2192 '{plan.into_name}'[/bold]")
        console.print(f"  mentions reassigned: {plan.mentions_reassigned}")
        console.print(f"  mentions de-duped (already on target): {plan.mentions_deduped}")
        console.print(f"  watch models reassigned: {plan.watch_models_reassigned}")
        console.print(
            f"  derived rows dropped: {plan.signals_deleted} signals, "
            f"{plan.scores_deleted} scores (recomputed next scan)"
        )
        console.print(f"  aliases added to target: {plan.aliases_added or 'none'}")
        if plan.done:
            console.print(
                "\n[green]Merged.[/green] Re-run a scan to rescore the surviving maker."
            )
        else:
            console.print("\n[yellow]Dry run.[/yellow] Re-run with --commit to apply.")


@app.command()
def unalias(
    maker: str = typer.Argument(..., help="Maker (id or name) to remove an alias from."),
    alias: str = typer.Argument(..., help="The alias string to remove."),
    commit: bool = typer.Option(
        False, "--commit", help="Apply the removal. Dry-run otherwise."
    ),
) -> None:
    """Remove a bad alias from a maker (e.g. a wrong fuzzy auto-merge)."""
    from watch_app.extraction.maker_admin import remove_alias

    with session_scope() as session:
        res = remove_alias(session, maker, alias, commit=commit)
        for blocker in res.blockers:
            console.print(f"[red]\u2717 {blocker}[/red]")
        if res.blockers:
            raise typer.Exit(1)
        verb = "Removed" if res.removed else "Would remove"
        console.print(f"[bold]{verb} alias '{res.alias}' from '{res.maker_name}'.[/bold]")
        if res.seed_warning:
            console.print(
                "[yellow]\u26a0 This is a seed maker. If the alias is defined in "
                "known_makers.yaml it will be re-added on the next scan — remove it "
                "there too.[/yellow]"
            )
        if not res.removed:
            console.print("\n[yellow]Dry run.[/yellow] Re-run with --commit to apply.")


@app.command()
def delete(
    maker: str = typer.Argument(
        ..., help="Maker (id or name) to delete — must be a discovered maker."
    ),
    commit: bool = typer.Option(
        False, "--commit", help="Apply the deletion. Dry-run otherwise."
    ),
) -> None:
    """Delete a discovered maker and all rows that reference it.

    Dry-run by default. Refuses seed makers — remove the known_makers.yaml entry instead.
    """
    from watch_app.extraction.maker_admin import delete_maker

    with session_scope() as session:
        plan = delete_maker(session, maker, commit=commit)
        for blocker in plan.blockers:
            console.print(f"[red]\u2717 {blocker}[/red]")
        if plan.blockers:
            raise typer.Exit(1)

        console.print(f"[bold]Delete '{plan.maker_name}'[/bold]")
        console.print(f"  mentions deleted: {plan.mentions_deleted}")
        console.print(f"  watch models deleted: {plan.watch_models_deleted}")
        console.print(
            f"  derived rows dropped: {plan.signals_deleted} signals, "
            f"{plan.scores_deleted} scores"
        )
        if plan.done:
            console.print("\n[green]Deleted.[/green]")
        else:
            console.print("\n[yellow]Dry run.[/yellow] Re-run with --commit to apply.")


@app.command()
def suppress(
    maker: str = typer.Argument(
        ..., help="Maker (id or name) to hide from discoveries + rankings."
    ),
    reason: str = typer.Option(
        None, "--reason", help="Why it's being suppressed (shown in `suppressed`)."
    ),
    commit: bool = typer.Option(
        False, "--commit", help="Apply. Dry-run otherwise."
    ),
) -> None:
    """Hide a maker from discoveries and rankings without deleting it (reversible)."""
    from watch_app.extraction.maker_admin import suppress_maker

    with session_scope() as session:
        res = suppress_maker(session, maker, reason=reason, commit=commit)
        for blocker in res.blockers:
            console.print(f"[red]\u2717 {blocker}[/red]")
        if res.blockers:
            raise typer.Exit(1)
        if res.already_in_state:
            console.print(f"[yellow]'{res.maker_name}' is already suppressed.[/yellow]")
            return

        line = f"[bold]Suppress '{res.maker_name}'[/bold]"
        if res.reason:
            line += f" \u2014 {res.reason}"
        console.print(line)
        if res.done:
            console.print(
                "\n[green]Suppressed.[/green] It drops out of /discoveries and the rankings."
            )
        else:
            console.print("\n[yellow]Dry run.[/yellow] Re-run with --commit to apply.")


@app.command()
def unsuppress(
    maker: str = typer.Argument(..., help="Maker (id or name) to un-hide."),
    commit: bool = typer.Option(
        False, "--commit", help="Apply. Dry-run otherwise."
    ),
) -> None:
    """Reverse a suppression — the maker returns to discoveries and rankings."""
    from watch_app.extraction.maker_admin import unsuppress_maker

    with session_scope() as session:
        res = unsuppress_maker(session, maker, commit=commit)
        for blocker in res.blockers:
            console.print(f"[red]\u2717 {blocker}[/red]")
        if res.blockers:
            raise typer.Exit(1)
        if res.already_in_state:
            console.print(f"[yellow]'{res.maker_name}' is not suppressed.[/yellow]")
            return

        console.print(f"[bold]Unsuppress '{res.maker_name}'[/bold]")
        if res.done:
            console.print("\n[green]Restored.[/green]")
        else:
            console.print("\n[yellow]Dry run.[/yellow] Re-run with --commit to apply.")


@app.command()
def suppressed() -> None:
    """List makers currently hidden via `suppress`."""
    from watch_app.extraction.maker_admin import list_suppressed

    with session_scope() as session:
        makers = list_suppressed(session)
        if not makers:
            console.print("[green]No suppressed makers.[/green]")
            return
        table = Table(title="Suppressed makers")
        table.add_column("id", justify="right", style="dim")
        table.add_column("Maker")
        table.add_column("Reason", style="dim")
        for m in makers:
            table.add_row(str(m.id), m.canonical_name, m.suppressed_reason or "\u2014")
        console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
