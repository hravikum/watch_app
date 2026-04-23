"""Grand Prix d'Horlogerie de Genève (GPHG) scraper.

Fetches nominees from the official GPHG nominated-timepieces page for each
configured edition and yields one RawMention per (brand, watch) pair.

Why this source matters:
- GPHG nominations are a strong signal for the LV-Prize-adjacent discovery
  hunt. The academy of ~800 experts selects nominees, so the signal is
  orders of magnitude more curated than press coverage.
- This is a *structured* source — each mention arrives with a known brand
  already in `candidate_names`, so the pipeline skips extraction entirely.

Technical notes:
- The site is built with Next.js and returns data in React Server Component
  (RSC) Flight payloads embedded in <script>self.__next_f.push(...)</script>
  tags. We extract those script arguments, JSON-decode the inner string
  (which preserves UTF-8), and regex-match brand/watch patterns.
- No BeautifulSoup needed — the Flight payload has a predictable shape and
  the regex is faster and more robust than trying to parse JSX-ish markup.
- The extracted date for each nominee is set to the event year's typical
  announcement month (October for GPHG). That puts the mention in a sensible
  point in time for the recency and velocity signals.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Iterator

from watch_app.http import get as http_get
from watch_app.sources.base import RawMention, Source

logger = logging.getLogger(__name__)


#: Use a browser UA — GPHG's Next.js app has been confirmed to serve a full
#: RSC payload to Safari, which is what we want. A generic bot UA isn't
#: guaranteed to get the same response.
_GPHG_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


#: Each GPHG edition is announced in October of that year.
_ANNOUNCEMENT_MONTH = 10
_ANNOUNCEMENT_DAY = 15  # Mid-October; date is approximate by design.


#: Regex to find each self.__next_f.push([N,"..."]) call in the HTML.
_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[\d+,"(.*?)"\]\)',
    re.DOTALL,
)

#: Regex to find each (watch_href, brand, model) triple inside a decoded payload.
#: The payload contains box-main-title elements with this structure:
#:   "href":"/en/watches/<slug>"  ...  "h4",null,{"children":"<BRAND>"}
#:   ...  "__html":"<model name>"
_BOX_RE = re.compile(
    r'"box-main-title".*?'
    r'"href":"(/en/watches/[^"]+)".*?'
    r'"h4",null,\{"children":"([^"]+)"'
    r'.*?'
    r'"__html":"([^"]*)"',
    re.DOTALL,
)


def _decode_flight_string(raw: str) -> str | None:
    r"""JSON-decode a Next.js Flight payload string, preserving UTF-8.

    The raw string is what sits between the quotes in push([N,"..."]),
    which uses JSON string escape rules (\" for ", \\ for \, etc.).
    We re-wrap it in quotes and let json.loads decode properly — this
    preserves UTF-8 bytes (unlike .decode('unicode_escape') which
    corrupts multi-byte chars).
    """
    try:
        return json.loads('"' + raw + '"')
    except json.JSONDecodeError:
        return None


def _extract_nominees(html: str) -> list[tuple[str, str, str]]:
    """Extract (brand, model, watch_url) triples from the GPHG HTML.

    Returns a list because we want a deterministic count (for logging)
    before yielding RawMentions.
    """
    results: list[tuple[str, str, str]] = []
    for match in _PUSH_RE.finditer(html):
        payload = _decode_flight_string(match.group(1))
        if payload is None:
            continue
        for href, brand, model in _BOX_RE.findall(payload):
            # href is a path; make it absolute.
            url = f"https://www.gphg.org{href}"
            results.append((brand.strip(), model.strip(), url))
    return results


class GPHGSource(Source):
    """Scraper for a single GPHG edition year.

    Multiple years are supported by instantiating multiple times with
    different `year` values.
    """

    name: str
    category = "awards"
    #: GPHG nominations carry more weight than press mentions. Tunable.
    prestige_weight = 1.2

    def __init__(self, year: int):
        self.year = year
        self.name = f"GPHG {year}"
        self.url = f"https://www.gphg.org/en/gphg-{year}/nominated-timepieces"

    def fetch(self) -> Iterator[RawMention]:
        try:
            response = http_get(self.url, user_agent=_GPHG_USER_AGENT)
        except Exception:
            logger.exception("GPHG %s: fetch failed", self.year)
            return

        if response.status_code != 200:
            logger.warning(
                "GPHG %s: HTTP %s from %s",
                self.year, response.status_code, self.url,
            )
            return

        nominees = _extract_nominees(response.text)
        if not nominees:
            logger.warning(
                "GPHG %s: no nominees parsed from %s — site format may have changed",
                self.year, self.url,
            )
            return

        logger.info("GPHG %s: extracted %d nominees", self.year, len(nominees))

        # Announcement date: mid-October of the edition year.
        published = datetime(
            self.year, _ANNOUNCEMENT_MONTH, _ANNOUNCEMENT_DAY,
            tzinfo=timezone.utc,
        )

        for brand, model, watch_url in nominees:
            # Title case the brand — GPHG uses all-caps ("URBAN JÜRGENSEN"),
            # but our resolver handles case-insensitive matching. We send
            # title-case so new auto-created makers look presentable.
            display_brand = _title_case_brand(brand)

            yield RawMention(
                source_name=self.name,
                url=watch_url,
                title=f"GPHG {self.year} nominee — {display_brand}: {model}",
                excerpt=f"Nominated for GPHG {self.year}.",
                published_at=published,
                candidate_names=[display_brand],
            )


#: Known brands where the conventional capitalization isn't just title-case.
#: Maps UPPERCASE GPHG name → preferred display spelling. Add cases here as
#: they come up.
_BRAND_CAPITALIZATION: dict[str, str] = {
    "F.P. JOURNE": "F.P. Journe",
    "H. MOSER & CIE": "H. Moser & Cie.",
    "M.A.D. EDITIONS": "M.A.D. Editions",
    "MB&F": "MB&F",
    "MING": "MING",
    "L'EPÉE 1839": "L'Épée 1839",
    "FAM AL HUT": "Fam Al Hut",
    "J.N. SHAPIRO": "J.N. Shapiro",
    "JACOB & CO": "Jacob & Co.",
    "VAN CLEEF & ARPELS": "Van Cleef & Arpels",
    "TIFFANY & CO.": "Tiffany & Co.",
    "BOVET 1822": "Bovet 1822",
    "NOMOS GLASHÜTTE": "Nomos",  # Canonical name in seed is "Nomos".
    "CHRISTOPHER WARD LONDON": "Christopher Ward",
    "LOUIS VUITTON X VOUTILAINEN": "Voutilainen",  # Collaboration → attribute to Voutilainen (indie).
    "BEDA'A": "Beda'a",
    "GÉRALD GENTA": "Gérald Genta",
    "HERMÈS": "Hermès",
    "ANDERSEN GENÈVE": "Andersen Genève",
    "FIONA KRÜGER": "Fiona Krüger",
    # XRBY is styled all-caps per the brand's own usage; the full string
    # GPHG emits ("XRBY LA MONTRE D'ART") is a tagline rather than the
    # actual brand name, so we consolidate under just "XRBY".
    "XRBY LA MONTRE D'ART": "XRBY",
    "XRBY": "XRBY",
    # HYT styles the brand in all-caps (often "H⁰").
    "HYT": "HYT",
    # DOXA is all-caps as a stylistic choice.
    "DOXA": "DOXA",
}


def _title_case_brand(upper: str) -> str:
    """Convert 'URBAN JÜRGENSEN' → 'Urban Jürgensen', handling known exceptions.

    For brands with non-standard capitalization (F.P. Journe, MB&F, etc.),
    the override dict takes precedence. Otherwise, use Python's built-in
    title-casing which handles most diacritics correctly.
    """
    if upper in _BRAND_CAPITALIZATION:
        return _BRAND_CAPITALIZATION[upper]
    # title() handles spaces and hyphens correctly for most brand names.
    # Diacritics are preserved since we didn't lowercase first.
    return upper.title()
