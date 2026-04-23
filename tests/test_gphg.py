"""Tests for the GPHG awards source.

Parser tests use a real-shaped fixture (excerpt from the actual GPHG 2025
page) to verify extraction against true site output — not synthetic HTML.
Source-level tests mock http_get to avoid network calls.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from watch_app.sources.awards.gphg import (
    GPHGSource,
    _BRAND_CAPITALIZATION,
    _extract_nominees,
    _title_case_brand,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture() -> str:
    return (FIXTURES / "gphg_2025_sample.html").read_text()


# ---------- Parser tests ----------


def test_extract_nominees_finds_all_expected_brands():
    """Our fixture contains 5 nominees; all should be extracted."""
    html = _load_fixture()
    nominees = _extract_nominees(html)
    assert len(nominees) == 5
    brands = [brand for brand, _, _ in nominees]
    assert "AUDEMARS PIGUET" in brands
    assert "VOUTILAINEN" in brands
    assert "RAÚL PAGÈS" in brands
    assert "ARMIN STROM" in brands
    assert "HAZEMANN & MONNIN" in brands


def test_extract_preserves_utf8_characters():
    """UTF-8 characters (accents) must survive the Flight decode."""
    html = _load_fixture()
    nominees = _extract_nominees(html)
    brand_names = [brand for brand, _, _ in nominees]
    assert "RAÚL PAGÈS" in brand_names  # Ú and È preserved


def test_extract_decodes_ampersand_escape():
    """\\u0026 in the RSC payload should decode to & in the brand name."""
    html = _load_fixture()
    nominees = _extract_nominees(html)
    brand_names = [brand for brand, _, _ in nominees]
    assert "HAZEMANN & MONNIN" in brand_names


def test_extract_returns_absolute_urls():
    """The _extract_nominees helper produces full https://www.gphg.org/... URLs."""
    html = _load_fixture()
    nominees = _extract_nominees(html)
    urls = [url for _, _, url in nominees]
    for url in urls:
        assert url.startswith("https://www.gphg.org/en/watches/")


def test_extract_handles_empty_html():
    """No Flight payload → no nominees, no crash."""
    assert _extract_nominees("<html><body>nothing</body></html>") == []


def test_extract_handles_malformed_payload():
    """A push() call with malformed JSON inside shouldn't crash the parser."""
    html = 'self.__next_f.push([1,"not valid json \\x"]);'
    # Should gracefully return empty, not raise.
    assert _extract_nominees(html) == []


# ---------- Title-casing tests ----------


def test_title_case_simple_brand():
    assert _title_case_brand("ARMIN STROM") == "Armin Strom"


def test_title_case_preserves_diacritics():
    assert _title_case_brand("RAÚL PAGÈS") == "Raúl Pagès"


def test_title_case_overrides_for_known_brands():
    """Brands with non-standard capitalization use the override map."""
    assert _title_case_brand("F.P. JOURNE") == "F.P. Journe"
    assert _title_case_brand("MB&F") == "MB&F"
    assert _title_case_brand("H. MOSER & CIE") == "H. Moser & Cie."


def test_collab_maps_to_primary_indie():
    """Louis Vuitton x Voutilainen collab should attribute to Voutilainen
    (the indie) so the indie gets the signal credit."""
    assert _title_case_brand("LOUIS VUITTON X VOUTILAINEN") == "Voutilainen"


def test_nomos_glashutte_canonical():
    """GPHG uses 'NOMOS GLASHÜTTE'; our seed canonical is 'Nomos'."""
    assert _title_case_brand("NOMOS GLASHÜTTE") == "Nomos"


def test_xrby_tagline_maps_to_brand_name():
    """GPHG emits 'XRBY LA MONTRE D'ART' (brand + tagline); we consolidate
    under 'XRBY' which is the brand's own styling."""
    assert _title_case_brand("XRBY LA MONTRE D'ART") == "XRBY"


def test_all_caps_brands_preserve_styling():
    """Brands that style themselves all-caps (HYT, DOXA, MING, MB&F, XRBY)
    must not be title-cased — that would break visual identity."""
    assert _title_case_brand("HYT") == "HYT"
    assert _title_case_brand("DOXA") == "DOXA"
    assert _title_case_brand("MING") == "MING"
    assert _title_case_brand("MB&F") == "MB&F"
    assert _title_case_brand("XRBY") == "XRBY"


# ---------- GPHGSource tests ----------


class _FakeResponse:
    """Minimal response object matching the httpx.Response surface used."""

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def test_source_yields_one_mention_per_nominee():
    """GPHGSource.fetch() should produce one RawMention per nominee."""
    html = _load_fixture()
    source = GPHGSource(2025)
    with patch("watch_app.sources.awards.gphg.http_get") as mock_get:
        mock_get.return_value = _FakeResponse(html)
        mentions = list(source.fetch())
    assert len(mentions) == 5


def test_source_sets_candidate_names():
    """Each mention should have candidate_names prefilled — GPHG is a structured source."""
    html = _load_fixture()
    source = GPHGSource(2025)
    with patch("watch_app.sources.awards.gphg.http_get") as mock_get:
        mock_get.return_value = _FakeResponse(html)
        mentions = list(source.fetch())
    for m in mentions:
        assert len(m.candidate_names) == 1
        assert m.candidate_names[0]  # non-empty


def test_source_sets_published_year():
    """Published date should reflect the edition year, not today."""
    html = _load_fixture()
    source = GPHGSource(2025)
    with patch("watch_app.sources.awards.gphg.http_get") as mock_get:
        mock_get.return_value = _FakeResponse(html)
        mentions = list(source.fetch())
    for m in mentions:
        assert m.published_at.year == 2025


def test_source_handles_http_error():
    """Non-200 response → no mentions yielded, no crash."""
    source = GPHGSource(2025)
    with patch("watch_app.sources.awards.gphg.http_get") as mock_get:
        mock_get.return_value = _FakeResponse("", status_code=500)
        mentions = list(source.fetch())
    assert mentions == []


def test_source_handles_empty_parse():
    """HTML with no nominees parseable → empty iterator, no crash."""
    source = GPHGSource(2025)
    with patch("watch_app.sources.awards.gphg.http_get") as mock_get:
        mock_get.return_value = _FakeResponse("<html>no data</html>")
        mentions = list(source.fetch())
    assert mentions == []


def test_source_name_includes_year():
    """Each edition gets a distinct source name."""
    assert GPHGSource(2025).name == "GPHG 2025"
    assert GPHGSource(2024).name == "GPHG 2024"
    assert GPHGSource(2023).name == "GPHG 2023"


def test_source_prestige_above_rss():
    """GPHG should outweigh top-tier RSS sources (SJX=1.0, this=1.2)."""
    assert GPHGSource(2025).prestige_weight > 1.0


# ---------- Brand capitalization override dict consistency ----------


def test_all_override_keys_are_uppercase():
    """The override dict's keys should match what GPHG emits (all caps)."""
    for key in _BRAND_CAPITALIZATION:
        # Allow apostrophes, periods, spaces, ampersands, digits, diacritics.
        letters = [c for c in key if c.isalpha()]
        assert all(c.isupper() for c in letters), (
            f"Override key {key!r} contains lowercase letters — will never match"
        )
