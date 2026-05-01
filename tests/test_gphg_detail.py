"""Tests for the GPHG watch-detail parser.

Uses pre-built fixture HTML files in tests/fixtures/ to validate parsing
without hitting the network. Two fixtures cover the common shapes:

- gphg_detail_full.html: a recent fully-populated nominee with all spec
  fields, prices, and a limited-edition indicator
- gphg_detail_minimal.html: an older nominee with only the basics, plus
  a label we don't recognize (which should be silently skipped)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from watch_app.sources.awards.gphg_detail import (
    _parse_diameter_mm,
    _parse_frequency_hz,
    _parse_limited_edition,
    _parse_movement_type,
    _parse_power_reserve_hours,
    _parse_price_chf,
    _parse_water_resistance_m,
    parse_detail_page,
)


FIXTURES = Path(__file__).parent / "fixtures"


# ---------- Parser end-to-end ----------


def test_parse_full_detail_page():
    """Full fixture should yield every supported field."""
    html = (FIXTURES / "gphg_detail_full.html").read_text()
    out = parse_detail_page(html)

    assert out["case_material"] == "Polished and satin finished platinum 950"
    assert out["case_diameter_mm"] == 40.0
    assert out["water_resistance_m"] == 100
    assert "Self-winding mechanical" in out["movement_caliber"]
    assert "Power reserve" in out["movement_caliber"]
    assert out["movement_type"] == "automatic"
    assert out["frequency_hz"] == 3.0  # 21600 vph / 7200 = 3 Hz
    assert out["power_reserve_hours"] == 48
    assert out["complications"] == ["Hours", "Minutes"]
    assert out["price_chf"] == 85000
    assert out["limited_edition_count"] == 25
    assert "Tonda PF Micro-Rotor" in out["description"]
    # og:image extraction from <head>
    assert out["image_url"].startswith("https://")


def test_parse_minimal_detail_page():
    """Minimal fixture has only basics plus an unrecognized field. The
    parser should return what it can and silently skip the rest."""
    html = (FIXTURES / "gphg_detail_minimal.html").read_text()
    out = parse_detail_page(html)

    assert out["case_material"] == "Steel"
    assert out["complications"] == ["Hours", "Minutes", "Small seconds"]

    # Fields that should NOT appear because the data isn't there
    assert "case_diameter_mm" not in out
    assert "water_resistance_m" not in out
    assert "price_chf" not in out
    assert "limited_edition_count" not in out
    assert "frequency_hz" not in out
    assert "movement_caliber" not in out  # No movement block in minimal


def test_parse_empty_html_returns_empty_dict():
    """No Flight pushes → empty dict."""
    out = parse_detail_page("<html><body><p>No specs.</p></body></html>")
    assert out == {}


def test_parse_handles_malformed_html():
    """Garbage in shouldn't blow up."""
    out = parse_detail_page("<<<")
    assert isinstance(out, dict)


def test_parse_handles_unrecognized_label():
    """The minimal fixture includes a 'Strange Field We Don't Understand'
    entry. The parser should silently skip it without raising."""
    html = (FIXTURES / "gphg_detail_minimal.html").read_text()
    out = parse_detail_page(html)
    # Verify the unknown field didn't pollute any known field
    for v in out.values():
        if isinstance(v, str):
            assert "Unknown content" not in v


def test_parse_realistic_page():
    """Fixture mirroring the variant block shapes seen on a real GPHG
    detail page: dangerouslySetInnerHTML spans, mixed-array items with
    empty leading entries, nested-array items with <br> sub-elements,
    watch-detail-group-* wrappers, and the curly-quote price separator.

    Every shape must parse without resorting to per-page workarounds."""
    html = (FIXTURES / "gphg_detail_realistic.html").read_text()
    out = parse_detail_page(html)

    # dangerouslySetInnerHTML extraction
    assert out["case_material"] == "Platinum"
    # Plain-string field-items
    assert out["water_resistance_m"] == 100
    assert out["case_diameter_mm"] == 40.0
    # Curly-apostrophe thousands separator (U+2019) — must be 85000, not 85
    assert out["price_chf"] == 85000
    # Nested array with <br> sub-element
    assert out["complications"] == ["Hours", "Minutes"]
    # Movement group block
    assert out["movement_type"] == "automatic"
    assert out["frequency_hz"] == 3.0
    assert out["power_reserve_hours"] == 48


# ---------- Field-level normalizer tests ----------


@pytest.mark.parametrize("raw,expected", [
    ("ø 40 mm", 40.0),
    ("38mm", 38.0),
    ("38.5 mm", 38.5),
    ("Ø 41,5 mm", 41.5),
    ("40 mm thick", 40.0),
    ("no diameter here", None),
    ("", None),
])
def test_diameter_parsing(raw, expected):
    assert _parse_diameter_mm(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("100 m", 100),
    ("50m", 50),
    ("30 metres", 30),
    ("3 ATM", 30),
    ("10 bar", 100),
    ("not water resistant", None),
])
def test_water_resistance_parsing(raw, expected):
    assert _parse_water_resistance_m(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("85'000 CHF", 85000),
    ("120,000 CHF", 120000),
    ("85.000 CHF", 85000),
    ("85000 CHF", 85000),
    ("CHF 85,000", 85000),
    ("$50,000 USD", None),  # Not CHF
    ("EUR 100,000", None),
])
def test_price_chf_parsing(raw, expected):
    assert _parse_price_chf(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Self-winding mechanical", "automatic"),
    ("Self winding", "automatic"),
    ("Automatic with rotor", "automatic"),
    ("Hand-wound mechanical", "hand-wound"),
    ("Hand wound", "hand-wound"),
    ("Manually wound", "hand-wound"),
    ("Quartz movement", "quartz"),
    ("something else", None),
])
def test_movement_type_parsing(raw, expected):
    assert _parse_movement_type(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("21600 vph", 3.0),
    ("28800 A/h", 4.0),
    ("28'800 vph", 4.0),
    ("3 Hz", 3.0),
    ("4Hz", 4.0),
    ("4.5 Hz", 4.5),
    ("no frequency info", None),
])
def test_frequency_parsing(raw, expected):
    assert _parse_frequency_hz(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Power reserve: 48 h", 48),
    ("70 hours", 70),
    ("5 days", 120),
    ("3 day power reserve", 72),
    ("no power reserve mentioned", None),
])
def test_power_reserve_parsing(raw, expected):
    assert _parse_power_reserve_hours(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("25-pieces limited series", 25),
    ("100 pieces", 100),
    ("Limited to 50 pieces", 50),
    ("Open series", None),
])
def test_limited_edition_parsing(raw, expected):
    assert _parse_limited_edition(raw) == expected
