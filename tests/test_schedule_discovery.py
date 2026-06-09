"""Tests for the weekly LLM discovery scheduler command (chunk 4B).

The command only prints a launchd plist + instructions — no side effects — so
these assert the emitted plist runs the right command (scan --use-llm, tagged
trigger=discovery) on the chosen weekly cadence.
"""
from __future__ import annotations

from typer.testing import CliRunner

from watch_app.cli import app as cli_app

runner = CliRunner()


def test_schedule_discovery_defaults_to_monday_6am():
    res = runner.invoke(cli_app, ["schedule-discovery"])
    assert res.exit_code == 0
    out = res.stdout
    assert "com.user.watchapp.discovery" in out
    # Runs an LLM scan tagged as discovery, not the plain daily scan.
    assert "--use-llm" in out
    assert "discovery" in out
    assert "Monday" in out
    # Weekly cadence (not the daily scan's bare Hour/Minute), Monday=1, hour 6.
    assert "<key>Weekday</key>" in out
    assert "<integer>1</integer>" in out
    assert "<integer>6</integer>" in out
    # Distinct log target so discovery runs don't blur into scan.log.
    assert "discovery.log" in out


def test_schedule_discovery_custom_day_and_time():
    res = runner.invoke(
        cli_app, ["schedule-discovery", "--day", "sun", "--hour", "5", "--minute", "30"]
    )
    assert res.exit_code == 0
    out = res.stdout
    assert "Sunday" in out
    assert "<integer>0</integer>" in out   # Sunday = Weekday 0
    assert "<integer>5</integer>" in out   # hour
    assert "<integer>30</integer>" in out  # minute


def test_schedule_discovery_complements_not_replaces_daily():
    """The output should make clear it runs alongside the daily scan, and it
    must not be the digest job."""
    res = runner.invoke(cli_app, ["schedule-discovery"])
    out = res.stdout.lower()
    assert "daily" in out
    assert "com.user.watchapp.digest" not in res.stdout


def test_schedule_discovery_rejects_bad_day():
    res = runner.invoke(cli_app, ["schedule-discovery", "--day", "funday"])
    assert res.exit_code != 0


def test_schedule_discovery_rejects_bad_hour():
    res = runner.invoke(cli_app, ["schedule-discovery", "--hour", "99"])
    assert res.exit_code != 0
