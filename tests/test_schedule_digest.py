"""Tests for the weekly digest scheduler command (chunk 3U).

The command only prints a launchd plist + instructions — no side effects — so
these assert the emitted plist reflects the chosen cadence and runs the right
command.
"""
from __future__ import annotations

from typer.testing import CliRunner

from watch_app.cli import app as cli_app

runner = CliRunner()


def test_schedule_digest_defaults_to_monday_8am():
    res = runner.invoke(cli_app, ["schedule-digest"])
    assert res.exit_code == 0
    out = res.stdout
    assert "com.user.watchapp.digest" in out
    assert "send-digest" in out and "--send" in out
    assert "Monday" in out
    # Monday = Weekday 1, default hour 8.
    assert "<key>Weekday</key>" in out
    assert "<integer>1</integer>" in out
    assert "<integer>8</integer>" in out


def test_schedule_digest_custom_day_and_time():
    res = runner.invoke(cli_app, ["schedule-digest", "--day", "fri", "--hour", "18", "--minute", "30"])
    assert res.exit_code == 0
    out = res.stdout
    assert "Friday" in out
    assert "<integer>5</integer>" in out   # Friday
    assert "<integer>18</integer>" in out  # hour
    assert "<integer>30</integer>" in out  # minute


def test_schedule_digest_rejects_bad_day():
    res = runner.invoke(cli_app, ["schedule-digest", "--day", "funday"])
    assert res.exit_code != 0


def test_schedule_digest_rejects_bad_hour():
    res = runner.invoke(cli_app, ["schedule-digest", "--hour", "99"])
    assert res.exit_code != 0
