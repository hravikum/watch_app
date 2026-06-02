"""Weekly "what changed" digest.

A summary of what moved in a recent window, composed almost entirely from
data we already store — so this module is thin glue over existing queries
rather than new analysis:

  - New discoveries  → fetch_discovered_makers(since=cutoff)
  - Movers           → fetch_rank_changes (the /movers engine)
  - Watching activity → makers you've marked watching/target that had new
                        mentions or rank movement in the window

That third section is the point: it turns a global recap into a personal
"what happened on my list this week" alert, pairing with the relationship
state from chunk 3R. A watched maker with no activity in the window is
omitted, so the section stays signal — empty when nothing moved.

`compute_weekly_digest` returns a WeeklyDigest dataclass; both surfaces (the
`digest` CLI command and the /digest web page) render the same object, and
`render_digest_markdown` produces a portable export.

Windowing note: "in the window" means *entered our data* in the window —
first_seen_at for makers, discovered_at for mentions (ingest time, not the
article's publication date). That's the honest meaning of a changelog ("what
surfaced this week"), and it matches how /discoveries thinks about recency.
It is deliberately different from the velocity signal, which keys off
published_at to avoid letting day-1 historical backfill read as recent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from watch_app.config import get_settings
from watch_app.scoring.discovery import DiscoveredMaker, fetch_discovered_makers
from watch_app.scoring.presets import PRESETS
from watch_app.scoring.queries import fetch_rank_changes, fetch_top_makers
from watch_app.storage.models import Maker, Mention, Score

#: Relationship states that count as "my list" for the watching section.
_WATCHED_STATES = ("watching", "target")

#: Clamp window to a sane range (mirrors fetch_rank_changes).
_MIN_WINDOW, _MAX_WINDOW = 1, 30


@dataclass
class WatchingActivity:
    """One watched/target maker that did something in the window."""

    maker_id: int
    canonical_name: str
    country: str | None
    relationship_state: str
    new_mentions: int
    current_rank: int | None
    rank_change: int | None  # positive = climbed; None if no baseline / unranked
    newly_discovered: bool


@dataclass
class WeeklyDigest:
    """Everything the CLI and the web page need to render the digest."""

    window_days: int
    preset: str
    generated_at: datetime
    has_history: bool  # is there score history older than the window?
    new_discoveries: list[DiscoveredMaker] = field(default_factory=list)
    climbers: list[dict[str, Any]] = field(default_factory=list)
    fallers: list[dict[str, Any]] = field(default_factory=list)
    new_entrants: list[dict[str, Any]] = field(default_factory=list)
    watching: list[WatchingActivity] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing changed in the window worth reporting."""
        return not (
            self.new_discoveries
            or self.climbers
            or self.fallers
            or self.new_entrants
            or self.watching
        )


def compute_weekly_digest(
    session: Session,
    window_days: int = 7,
    preset: str | None = None,
) -> WeeklyDigest:
    """Build the digest for the last `window_days` under `preset`.

    `preset` defaults to the configured default; an unknown preset raises
    ValueError so callers can surface it rather than silently mis-rank.
    `window_days` is clamped to 1-30.
    """
    preset = preset or get_settings().default_preset
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}")
    window_days = max(_MIN_WINDOW, min(_MAX_WINDOW, window_days))

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    # Is there any score history older than the window? If not, every ranked
    # maker reads as a "new entrant" and there are no real climbers/fallers —
    # the surfaces flag this so a thin-history digest isn't mistaken for a
    # quiet week.
    has_history = (
        session.query(Score.id).filter(Score.computed_at <= cutoff).first()
        is not None
    )

    new_discoveries = fetch_discovered_makers(session, since=cutoff)
    new_discovery_ids = {d.id for d in new_discoveries}
    groups = fetch_rank_changes(session, preset, days=window_days)
    watching = _watching_activity(session, preset, cutoff, groups, new_discovery_ids)

    return WeeklyDigest(
        window_days=window_days,
        preset=preset,
        generated_at=now,
        has_history=has_history,
        new_discoveries=new_discoveries,
        climbers=groups["climbers"],
        fallers=groups["fallers"],
        new_entrants=groups["new_entrants"],
        watching=watching,
    )


def _watching_activity(
    session: Session,
    preset: str,
    cutoff: datetime,
    groups: dict[str, list[dict[str, Any]]],
    new_discovery_ids: set[int],
) -> list[WatchingActivity]:
    """Watched/target makers with activity (new mentions, rank movement, or
    a brand-new discovery) in the window. Stable, no-activity makers are
    omitted so the section reads as an alert, not a roster."""
    watched = (
        session.query(Maker)
        .filter(
            Maker.relationship_state.in_(_WATCHED_STATES),
            Maker.is_suppressed.isnot(True),
        )
        .all()
    )
    if not watched:
        return []

    watched_ids = [m.id for m in watched]

    # New mentions per watched maker in the window (ingest time).
    new_mentions = dict(
        session.query(Mention.maker_id, func.count(Mention.id))
        .filter(
            Mention.maker_id.in_(watched_ids),
            Mention.discovered_at >= cutoff,
        )
        .group_by(Mention.maker_id)
        .all()
    )

    # Rank movement: index the movers groups by maker. Current rank for any
    # ranked maker (mover or not) comes from the latest ranking.
    rank_change_by_maker: dict[int, int | None] = {}
    for row in groups["climbers"] + groups["fallers"]:
        rank_change_by_maker[row["maker_id"]] = row["rank_change"]
    mover_ids = {
        r["maker_id"]
        for r in groups["climbers"] + groups["fallers"] + groups["new_entrants"]
    }
    current_rank = {
        r["maker_id"]: r["rank"]
        for r in fetch_top_makers(session, preset, limit=100_000)
    }

    out: list[WatchingActivity] = []
    for m in watched:
        n_mentions = int(new_mentions.get(m.id, 0))
        is_mover = m.id in mover_ids
        newly_discovered = m.id in new_discovery_ids
        if not (n_mentions or is_mover or newly_discovered):
            continue  # no activity this window → not an alert
        out.append(
            WatchingActivity(
                maker_id=m.id,
                canonical_name=m.canonical_name,
                country=m.country,
                relationship_state=m.relationship_state,
                new_mentions=n_mentions,
                current_rank=current_rank.get(m.id),
                rank_change=rank_change_by_maker.get(m.id),
                newly_discovered=newly_discovered,
            )
        )

    # Most actionable first: brand-new, then most press, then biggest move.
    out.sort(
        key=lambda a: (
            a.newly_discovered,
            a.new_mentions,
            abs(a.rank_change or 0),
        ),
        reverse=True,
    )
    return out


# --------------------------------------------------------------------------
# Markdown rendering (portable export — shared by `digest --markdown`)
# --------------------------------------------------------------------------


def _fmt_change(rank_change: int | None) -> str:
    if rank_change is None:
        return "new"
    if rank_change > 0:
        return f"+{rank_change}"
    if rank_change < 0:
        return str(rank_change)
    return "—"


def render_digest_markdown(digest: WeeklyDigest) -> str:
    """Render a WeeklyDigest as a portable markdown document."""
    d = digest
    lines: list[str] = []
    stamp = d.generated_at.strftime("%Y-%m-%d")
    lines.append(f"# What changed — last {d.window_days} days")
    lines.append("")
    lines.append(f"_Preset: {d.preset} · generated {stamp}_")
    lines.append("")

    if not d.has_history:
        lines.append(
            "> No score history older than this window yet — rank movement "
            "below is provisional (everything ranked reads as a new entrant)."
        )
        lines.append("")

    if d.is_empty:
        lines.append("Nothing surfaced in this window.")
        lines.append("")
        return "\n".join(lines)

    # New discoveries
    lines.append(f"## New discoveries ({len(d.new_discoveries)})")
    lines.append("")
    if d.new_discoveries:
        for m in d.new_discoveries:
            country = f" · {m.country}" if m.country else ""
            lines.append(
                f"- **{m.canonical_name}**{country} — "
                f"{m.mention_count} mention(s), via {', '.join(m.source_names) or '—'}"
            )
    else:
        lines.append("_None._")
    lines.append("")

    # Watching activity (the personal alert)
    lines.append(f"## On your list ({len(d.watching)})")
    lines.append("")
    if d.watching:
        for a in d.watching:
            bits = [f"_{a.relationship_state}_"]
            if a.newly_discovered:
                bits.append("newly discovered")
            if a.new_mentions:
                bits.append(f"{a.new_mentions} new mention(s)")
            if a.rank_change:
                bits.append(f"rank {_fmt_change(a.rank_change)}")
            elif a.current_rank is not None:
                bits.append(f"rank #{a.current_rank}")
            lines.append(f"- **{a.canonical_name}** — {', '.join(bits)}")
    else:
        lines.append("_No movement on watched or target makers._")
    lines.append("")

    # Movers
    lines.append(f"## Movers — {d.preset}")
    lines.append("")
    _md_mover_block(lines, "Climbers", d.climbers)
    _md_mover_block(lines, "Fallers", d.fallers)
    _md_mover_block(lines, "New entrants", d.new_entrants, entrants=True)

    return "\n".join(lines).rstrip() + "\n"


def _md_mover_block(
    lines: list[str], title: str, rows: list[dict[str, Any]], entrants: bool = False
) -> None:
    lines.append(f"### {title} ({len(rows)})")
    if not rows:
        lines.append("_None._")
        lines.append("")
        return
    for r in rows:
        if entrants:
            lines.append(f"- **{r['canonical_name']}** — entered at #{r['current_rank']}")
        else:
            lines.append(
                f"- **{r['canonical_name']}** — #{r['prior_rank']} → "
                f"#{r['current_rank']} ({_fmt_change(r['rank_change'])})"
            )
    lines.append("")
