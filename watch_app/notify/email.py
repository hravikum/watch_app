"""Email delivery for the weekly digest.

Builds a *summarized* HTML email body (inline-styled, table-based so it
survives email clients) with the full digest attached as a self-contained,
nicely-styled HTML file, and sends it over stdlib SMTP.

Layering for testability and safety:

  - `render_summary_html` / `render_attachment_html` are pure string renderers.
  - `build_digest_message` assembles a multipart EmailMessage (plain-text
    markdown + HTML alternative + HTML attachment) — no network.
  - `send_digest` ties it together: it computes the digest, builds the message,
    and EITHER writes the artifacts to disk (dry_run, the default) OR delivers
    via SMTP. Dry-run is the default so a misconfigured cron job never sends
    surprise mail, and so you can preview before wiring credentials.

Credentials and recipients come from Settings (env vars) — never hardcoded.
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any

from watch_app.config import Settings, get_settings
from watch_app.scoring.digest import (
    WeeklyDigest,
    _fmt_change,
    render_digest_markdown,
)

# Palette (matches the web UI's stone/amber), inlined since email clients
# strip <style>/external CSS from the body.
_INK = "#1c1917"
_MUTED = "#a8a29e"
_LABEL = "#78716c"
_BORDER = "#e7e5e4"
_GREEN = "#15803d"
_RED = "#b91c1c"
_AMBER_BG = "#fffbeb"
_AMBER_BORDER = "#fde68a"
_AMBER_INK = "#b45309"
_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------


@dataclass
class SendResult:
    sent: bool
    recipient: str | None
    subject: str
    body_path: Path | None = None
    attachment_path: Path | None = None
    missing_config: list[str] | None = None  # set when we couldn't send


# --------------------------------------------------------------------------
# Subject + small helpers
# --------------------------------------------------------------------------


def _subject(d: WeeklyDigest) -> str:
    stamp = d.generated_at.strftime("%b %d")
    n_new = len(d.new_discoveries)
    n_list = len(d.watching)
    return f"Watch digest: {n_new} new, {n_list} on your list \u00b7 {stamp}"


def _rank_cell_html(rank_change: int | None, current_rank: int | None) -> str:
    if rank_change is not None and rank_change > 0:
        return f'<span style="color:{_GREEN};font-weight:600">&uarr; {rank_change}</span>'
    if rank_change is not None and rank_change < 0:
        return f'<span style="color:{_RED};font-weight:600">&darr; {-rank_change}</span>'
    if current_rank is not None:
        return f'<span style="color:{_MUTED}">#{current_rank}</span>'
    return f'<span style="color:{_BORDER}">&mdash;</span>'


# --------------------------------------------------------------------------
# Summarized HTML email body (inline-styled)
# --------------------------------------------------------------------------


def render_summary_html(digest: WeeklyDigest, cap: int = 5) -> str:
    """A compact, inline-styled HTML body. Leads with the personal section."""
    d = digest
    p = []
    p.append(
        f'<div style="font-family:{_FONT};max-width:600px;color:{_INK};'
        'font-size:14px;line-height:1.5">'
    )
    p.append(
        f'<h1 style="font-size:18px;margin:0 0 2px">What changed '
        f'&mdash; last {d.window_days} days</h1>'
    )
    p.append(
        f'<div style="color:{_LABEL};font-size:12px;margin-bottom:16px">'
        f'Preset: {escape(d.preset)} &middot; '
        f'{d.generated_at.strftime("%Y-%m-%d")}</div>'
    )

    if not d.has_history:
        p.append(
            f'<div style="background:{_AMBER_BG};border:1px solid {_AMBER_BORDER};'
            f'color:{_AMBER_INK};font-size:12px;padding:8px 10px;border-radius:8px;'
            'margin-bottom:16px">No score baseline older than this window yet '
            "&mdash; rank movement below is provisional.</div>"
        )

    if d.is_empty:
        p.append(
            f'<p style="color:{_MUTED}">Nothing surfaced in this window.</p></div>'
        )
        return "".join(p)

    # On your list (full)
    p.append(_section_heading("On your list", len(d.watching)))
    if d.watching:
        rows = []
        for a in d.watching:
            bits = [escape(a.relationship_state)]
            if a.newly_discovered:
                bits.append("new")
            label = " &middot; ".join(bits)
            mentions = f"{a.new_mentions} new" if a.new_mentions else "&mdash;"
            rows.append(
                _row(
                    escape(a.canonical_name),
                    label,
                    mentions,
                    _rank_cell_html(a.rank_change, a.current_rank),
                )
            )
        p.append(_table(["Maker", "Status", "Mentions", "Rank"], rows))
    else:
        p.append(
            f'<p style="color:{_MUTED};font-size:13px;margin:0 0 16px">'
            "No movement on watched or target makers.</p>"
        )

    # New discoveries (capped)
    p.append(_section_heading("New discoveries", len(d.new_discoveries)))
    if d.new_discoveries:
        rows = []
        for m in d.new_discoveries[:cap]:
            rows.append(
                _row(
                    escape(m.canonical_name),
                    escape(m.country or "\u2014"),
                    f"{m.mention_count}",
                )
            )
        p.append(_table(["Maker", "Country", "Mentions"], rows))
        p.append(_more(len(d.new_discoveries), cap))
    else:
        p.append(_none())

    # Movers (each capped)
    p.append(_section_heading(f"Movers \u2014 {d.preset}", None))
    p.append(_mover_block("Climbers", d.climbers, cap))
    p.append(_mover_block("Fallers", d.fallers, cap))
    p.append(_mover_block("New entrants", d.new_entrants, cap, entrants=True))

    p.append(
        f'<p style="color:{_MUTED};font-size:12px;margin-top:20px">'
        "Full digest attached as HTML &mdash; open it in a browser.</p>"
    )
    p.append("</div>")
    return "".join(p)


def _section_heading(title: str, count: int | None) -> str:
    c = (
        f' <span style="color:{_MUTED};font-weight:400">({count})</span>'
        if count is not None
        else ""
    )
    return (
        f'<div style="text-transform:uppercase;letter-spacing:0.1em;'
        f'font-size:11px;font-weight:600;color:{_LABEL};margin:18px 0 8px">'
        f"{escape(title)}{c}</div>"
    )


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(
        f'<th align="left" style="font-weight:600;color:{_LABEL};font-size:11px;'
        f'text-transform:uppercase;letter-spacing:0.08em;padding:0 12px 6px 0">'
        f"{escape(h)}</th>"
        for h in headers
    )
    return (
        '<table cellpadding="0" cellspacing="0" style="width:100%;'
        f'border-collapse:collapse;margin-bottom:8px"><tr>{head}</tr>'
        + "".join(rows)
        + "</table>"
    )


def _row(*cells: str) -> str:
    tds = "".join(
        f'<td style="padding:4px 12px 4px 0;border-top:1px solid {_BORDER};'
        f'font-size:13px;vertical-align:top">{c}</td>'
        for c in cells
    )
    return f"<tr>{tds}</tr>"


def _more(total: int, cap: int) -> str:
    if total <= cap:
        return ""
    return (
        f'<div style="color:{_MUTED};font-size:12px;margin:0 0 8px">'
        f"+{total - cap} more &mdash; see the attachment.</div>"
    )


def _none() -> str:
    return f'<p style="color:{_MUTED};font-size:13px;margin:0 0 8px">None.</p>'


def _mover_block(
    title: str, rows: list[dict[str, Any]], cap: int, entrants: bool = False
) -> str:
    p = [
        f'<div style="font-size:12px;font-weight:600;color:{_LABEL};margin:10px 0 4px">'
        f"{escape(title)} ({len(rows)})</div>"
    ]
    if not rows:
        p.append(_none())
        return "".join(p)
    html_rows = []
    for r in rows[:cap]:
        if entrants:
            move = f'entered at #{r["current_rank"]}'
        else:
            move = (
                f'#{r["prior_rank"]} &rarr; #{r["current_rank"]} '
                f"({escape(_fmt_change(r['rank_change']))})"
            )
        html_rows.append(
            _row(
                escape(r["canonical_name"]),
                f'<span style="color:{_MUTED}">{move}</span>',
            )
        )
    p.append(_table(["Maker", "Move"], html_rows))
    p.append(_more(len(rows), cap))
    return "".join(p)


# --------------------------------------------------------------------------
# Full HTML attachment (self-contained document — <style> is fine in a file)
# --------------------------------------------------------------------------


def render_attachment_html(digest: WeeklyDigest) -> str:
    """The full digest as a standalone, styled HTML document."""
    d = digest
    stamp = d.generated_at.strftime("%Y-%m-%d")
    css = """
      body{font-family:""" + _FONT + """;color:#1c1917;max-width:760px;
        margin:32px auto;padding:0 20px;line-height:1.55;background:#fafaf9}
      h1{font-size:24px;margin:0 0 4px} .sub{color:#78716c;font-size:13px;margin-bottom:24px}
      h2{font-size:12px;text-transform:uppercase;letter-spacing:0.12em;color:#78716c;
        margin:28px 0 10px;font-weight:600}
      table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e7e5e4;
        border-radius:10px;overflow:hidden;font-size:14px}
      th{text-align:left;background:#f5f5f4;color:#78716c;font-size:11px;text-transform:uppercase;
        letter-spacing:0.08em;padding:8px 14px;font-weight:600}
      td{padding:8px 14px;border-top:1px solid #f0efed} .muted{color:#a8a29e}
      .up{color:#15803d;font-weight:600} .down{color:#b91c1c;font-weight:600}
      .note{background:#fffbeb;border:1px solid #fde68a;color:#b45309;font-size:13px;
        padding:10px 12px;border-radius:8px;margin:0 0 16px}
      .none{color:#a8a29e;font-size:13px}
    """
    out = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>What changed \u2014 {stamp}</title>",
        f"<style>{css}</style></head><body>",
        f"<h1>What changed \u2014 last {d.window_days} days</h1>",
        f'<div class="sub">Preset: {escape(d.preset)} &middot; generated {stamp}</div>',
    ]
    if not d.has_history:
        out.append(
            '<div class="note">No score baseline older than this window yet '
            "&mdash; rank movement is provisional.</div>"
        )
    if d.is_empty:
        out.append('<p class="none">Nothing surfaced in this window.</p></body></html>')
        return "".join(out)

    # On your list
    out.append(f"<h2>On your list ({len(d.watching)})</h2>")
    if d.watching:
        out.append("<table><tr><th>Maker</th><th>Status</th><th>New mentions</th><th>Rank</th></tr>")
        for a in d.watching:
            status = escape(a.relationship_state) + (" &middot; new" if a.newly_discovered else "")
            out.append(
                f"<tr><td>{escape(a.canonical_name)}</td><td>{status}</td>"
                f"<td>{a.new_mentions or '&mdash;'}</td>"
                f"<td>{_rank_cell_html(a.rank_change, a.current_rank)}</td></tr>"
            )
        out.append("</table>")
    else:
        out.append('<p class="none">No movement on watched or target makers.</p>')

    # New discoveries
    out.append(f"<h2>New discoveries ({len(d.new_discoveries)})</h2>")
    if d.new_discoveries:
        out.append("<table><tr><th>Maker</th><th>Country</th><th>Mentions</th><th>Sources</th></tr>")
        for m in d.new_discoveries:
            out.append(
                f"<tr><td>{escape(m.canonical_name)}</td>"
                f"<td>{escape(m.country or '&mdash;')}</td>"
                f"<td>{m.mention_count}</td>"
                f"<td class='muted'>{escape(', '.join(m.source_names) or '&mdash;')}</td></tr>"
            )
        out.append("</table>")
    else:
        out.append('<p class="none">None.</p>')

    # Movers
    out.append(f"<h2>Movers \u2014 {escape(d.preset)}</h2>")
    if d.climbers or d.fallers or d.new_entrants:
        out.append("<table><tr><th>Change</th><th>Maker</th><th>Was</th><th>Now</th></tr>")
        for r in d.climbers:
            out.append(_attach_mover_row(r, f'<span class="up">&uarr; {r["rank_change"]}</span>'))
        for r in d.fallers:
            out.append(_attach_mover_row(r, f'<span class="down">&darr; {-r["rank_change"]}</span>'))
        for r in d.new_entrants:
            out.append(_attach_mover_row(r, '<span class="muted">new</span>'))
        out.append("</table>")
    else:
        out.append('<p class="none">No rank movement of 3+ positions.</p>')

    out.append("</body></html>")
    return "".join(out)


def _attach_mover_row(r: dict[str, Any], change_html: str) -> str:
    was = f'#{r["prior_rank"]}' if r.get("prior_rank") else "&mdash;"
    return (
        f"<tr><td>{change_html}</td><td>{escape(r['canonical_name'])}</td>"
        f"<td class='muted'>{was}</td><td>#{r['current_rank']}</td></tr>"
    )


# --------------------------------------------------------------------------
# MIME assembly
# --------------------------------------------------------------------------


def build_digest_message(
    digest: WeeklyDigest,
    sender: str,
    recipient: str,
    cap: int = 5,
) -> EmailMessage:
    """Assemble the multipart message: markdown text + HTML body + HTML file."""
    msg = EmailMessage()
    msg["Subject"] = _subject(digest)
    msg["From"] = sender
    msg["To"] = recipient
    # Plain-text fallback (the markdown export) + HTML alternative.
    msg.set_content(render_digest_markdown(digest))
    msg.add_alternative(render_summary_html(digest, cap=cap), subtype="html")
    # Full digest as an attached HTML file.
    fname = f"watch-digest-{digest.generated_at.strftime('%Y-%m-%d')}.html"
    msg.add_attachment(
        render_attachment_html(digest).encode("utf-8"),
        maintype="text",
        subtype="html",
        filename=fname,
    )
    return msg


# --------------------------------------------------------------------------
# Send (or dry-run)
# --------------------------------------------------------------------------


def _resolve_recipient(settings: Settings, to: str | None) -> str | None:
    return to or settings.digest_email_to or settings.smtp_user


def _resolve_sender(settings: Settings) -> str | None:
    return settings.digest_email_from or settings.smtp_user


def _missing_send_config(settings: Settings, recipient: str | None) -> list[str]:
    missing = []
    if not settings.smtp_host:
        missing.append("WATCH_APP_SMTP_HOST")
    if not settings.smtp_user:
        missing.append("WATCH_APP_SMTP_USER")
    if not settings.smtp_password:
        missing.append("WATCH_APP_SMTP_PASSWORD")
    if not recipient:
        missing.append("WATCH_APP_DIGEST_TO (or --to)")
    return missing


def _deliver(msg: EmailMessage, settings: Settings) -> None:
    """Open an SMTP connection and send. Isolated so tests can monkeypatch
    smtplib without exercising real network."""
    if settings.smtp_use_ssl:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)


def send_digest(
    digest: WeeklyDigest,
    to: str | None = None,
    dry_run: bool = True,
    out_dir: str | Path | None = None,
    cap: int = 5,
    settings: Settings | None = None,
) -> SendResult:
    """Build and either preview (dry_run) or send the digest email.

    dry_run (default) writes the HTML body and the HTML attachment to
    `out_dir` (default: the app data dir) and returns their paths without
    touching the network. A real send requires SMTP host/user/password and a
    recipient; if any are missing the result lists them and nothing is sent.
    """
    settings = settings or get_settings()
    recipient = _resolve_recipient(settings, to)
    sender = _resolve_sender(settings) or "watch-app@localhost"

    # Build with placeholders that still render even if unconfigured (dry-run).
    msg = build_digest_message(
        digest, sender=sender, recipient=recipient or "you@example.com", cap=cap
    )
    subject = msg["Subject"]

    if dry_run:
        base = Path(out_dir) if out_dir else settings.data_dir
        base.mkdir(parents=True, exist_ok=True)
        stamp = digest.generated_at.strftime("%Y-%m-%d")
        body_path = base / f"digest-email-{stamp}.html"
        att_path = base / f"watch-digest-{stamp}.html"
        body_path.write_text(render_summary_html(digest, cap=cap), encoding="utf-8")
        att_path.write_text(render_attachment_html(digest), encoding="utf-8")
        return SendResult(
            sent=False,
            recipient=recipient,
            subject=subject,
            body_path=body_path,
            attachment_path=att_path,
        )

    missing = _missing_send_config(settings, recipient)
    if missing:
        return SendResult(
            sent=False, recipient=recipient, subject=subject, missing_config=missing
        )

    _deliver(msg, settings)
    return SendResult(sent=True, recipient=recipient, subject=subject)
