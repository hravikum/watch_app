"""Tests for the digest email task (chunk 3T).

No real mail is sent: the dry-run path writes preview files, the live path is
exercised with a fake SMTP class monkeypatched in. Credentials come from a
Settings built off monkeypatched env vars — never hardcoded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from watch_app.cli import app as cli_app
from watch_app.config import Settings
from watch_app.notify import email as email_mod
from watch_app.notify.email import (
    build_digest_message,
    render_attachment_html,
    render_summary_html,
    send_digest,
)
from watch_app.scoring.digest import compute_weekly_digest
from watch_app.storage.db import session_scope
from watch_app.storage.models import Maker, Mention, Score, Source as SourceRow

NOW = datetime.now(timezone.utc)


def _seed_digest(s):
    src = SourceRow(name="Hodinkee", category="media", prestige_weight=0.8)
    s.add(src)
    s.flush()
    watched = Maker(canonical_name="Zzz Watched", is_independent=True,
                    relationship_state="watching", first_seen_at=NOW - timedelta(days=90))
    s.add_all([watched])
    s.flush()
    s.add(Mention(maker_id=watched.id, source_id=src.id, url="https://h/w",
                  title="news", discovered_at=NOW - timedelta(days=1)))
    # Seven discovered makers so the cap=5 overflow path triggers.
    for i in range(7):
        s.add(Maker(canonical_name=f"Zzz Disc {i}", is_independent=True,
                    first_seen_at=NOW - timedelta(days=2)))


def _digest():
    with session_scope() as s:
        _seed_digest(s)
    with session_scope() as s:
        return compute_weekly_digest(s, window_days=7)


# ---------- renderers ----------


def test_summary_html_has_sections_and_cap(test_engine):
    d = _digest()
    html = render_summary_html(d, cap=5)
    assert "What changed" in html
    assert "On your list" in html
    assert "New discoveries" in html
    assert "Zzz Watched" in html
    # 7 discoveries, cap 5 → an overflow note.
    assert "+2 more" in html


def test_attachment_html_is_full_document(test_engine):
    d = _digest()
    html = render_attachment_html(d)
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    # Full attachment shows every discovery (no cap), e.g. the 7th.
    assert "Zzz Disc 6" in html


def test_renderers_handle_empty(test_engine):
    with session_scope() as s:
        d = compute_weekly_digest(s, window_days=7)
    assert "Nothing surfaced" in render_summary_html(d)
    assert "Nothing surfaced" in render_attachment_html(d)


# ---------- MIME assembly ----------


def test_build_message_structure(test_engine):
    d = _digest()
    msg = build_digest_message(d, sender="from@x.com", recipient="to@y.com")
    assert msg["To"] == "to@y.com"
    assert msg["From"] == "from@x.com"
    assert "Watch digest" in msg["Subject"]
    assert msg.get_content_type() == "multipart/mixed"

    types = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in types  # markdown fallback
    assert "text/html" in types   # summary body + attachment are html

    attachments = [p for p in msg.walk() if p.get_filename()]
    assert len(attachments) == 1
    assert attachments[0].get_filename().endswith(".html")


# ---------- dry run ----------


def test_dry_run_writes_files_and_sends_nothing(test_engine, tmp_path):
    d = _digest()
    res = send_digest(d, dry_run=True, out_dir=tmp_path, settings=Settings())
    assert res.sent is False
    assert res.body_path.exists()
    assert res.attachment_path.exists()
    assert "What changed" in res.body_path.read_text()
    assert res.attachment_path.read_text().startswith("<!doctype html>")


# ---------- live send (mocked SMTP) ----------


class _FakeSMTP:
    """Records login + send_message; usable as a context manager."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.logged_in = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def test_live_send_uses_smtp(test_engine, monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setenv("WATCH_APP_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("WATCH_APP_SMTP_PORT", "465")
    monkeypatch.setenv("WATCH_APP_SMTP_USER", "me@example.com")
    monkeypatch.setenv("WATCH_APP_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("WATCH_APP_DIGEST_TO", "inbox@example.com")

    d = _digest()
    res = send_digest(d, dry_run=False, settings=Settings())

    assert res.sent is True
    assert res.recipient == "inbox@example.com"
    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.com" and smtp.port == 465
    assert smtp.logged_in == ("me@example.com", "secret")
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == "inbox@example.com"


def test_live_send_missing_config_reports_and_skips(test_engine, monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _FakeSMTP)
    # No SMTP env set → Settings() has all None.
    d = _digest()
    res = send_digest(d, dry_run=False, settings=Settings())
    assert res.sent is False
    assert res.missing_config  # lists the missing vars
    assert "WATCH_APP_SMTP_HOST" in res.missing_config
    assert not _FakeSMTP.instances  # never connected


# ---------- CLI ----------


def test_cli_send_digest_dry_run(test_engine, tmp_path):
    with session_scope() as s:
        _seed_digest(s)
    res = CliRunner().invoke(
        cli_app, ["send-digest", "--out", str(tmp_path)]
    )
    assert res.exit_code == 0
    assert "Dry run" in res.stdout
    assert list(tmp_path.glob("digest-email-*.html"))
