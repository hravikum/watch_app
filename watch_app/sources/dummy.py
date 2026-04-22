"""A dummy source with hardcoded data about real independent watchmakers.

This exists so Phase 0 can run the full pipeline end-to-end without needing
any real scrapers. Phase 1 will add real sources (GPHG, Monochrome, etc.) and
this module can either stay for tests or be deleted.

The makers chosen here are all real independent watchmakers who have been
recognized by the Louis Vuitton Watch Prize or similar, so the output looks
realistic when you run `watch-app top`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

from watch_app.sources.base import RawMention, Source


class DummySource(Source):
    """Yields a handful of pre-baked mentions about real indie watchmakers."""

    name = "Dummy Seed Data"
    category = "awards"
    prestige_weight = 1.0

    def fetch(self) -> Iterator[RawMention]:
        now = datetime.now(timezone.utc)

        # Past LV Prize winner (Edition 1, 2024)
        yield RawMention(
            source_name=self.name,
            url="https://example.com/raul-pages-wins-lv-prize",
            title="Raúl Pagès wins the first Louis Vuitton Watch Prize",
            excerpt=(
                "Independent Swiss watchmaker Raúl Pagès has been named the winner "
                "of the first Louis Vuitton Watch Prize for Independent Creatives "
                "for his Régulateur à Détente RP1."
            ),
            published_at=now - timedelta(days=600),
            candidate_names=["Raúl Pagès"],
        )

        # 2026 finalist
        yield RawMention(
            source_name=self.name,
            url="https://example.com/daizoh-makihara-beauties-of-nature",
            title="Daizoh Makihara's Beauties of Nature: a finalist's tour de force",
            excerpt=(
                "Japanese independent Daizoh Makihara has been selected as a "
                "finalist for the 2026 Louis Vuitton Watch Prize with his "
                "Beauties of Nature wristwatch."
            ),
            published_at=now - timedelta(days=60),
            candidate_names=["Daizoh Makihara"],
        )

        # 2024 finalist, strong rising-star signal
        yield RawMention(
            source_name=self.name,
            url="https://example.com/simon-brette-chronomètre-artisans",
            title="Simon Brette — independent watchmaker since 2022",
            excerpt=(
                "Simon Brette founded his eponymous atelier in 2022 and was named "
                "a finalist for the first Louis Vuitton Watch Prize."
            ),
            published_at=now - timedelta(days=400),
            candidate_names=["Simon Brette"],
        )

        # Multiple mentions of the same maker to make scoring interesting
        yield RawMention(
            source_name=self.name,
            url="https://example.com/simon-brette-follow-up",
            title="Simon Brette's second release draws collector attention",
            excerpt=(
                "Following his finalist nod, Simon Brette's latest timepiece has "
                "attracted significant interest from independent watch collectors."
            ),
            published_at=now - timedelta(days=120),
            candidate_names=["Simon Brette"],
        )

        # 2024 finalist duo
        yield RawMention(
            source_name=self.name,
            url="https://example.com/petermann-bedat",
            title="Petermann Bédat — a rising force in independent horology",
            excerpt=(
                "Gaël Petermann and Florian Bédat, independent since 2017, were "
                "finalists for the first Louis Vuitton Watch Prize."
            ),
            published_at=now - timedelta(days=500),
            candidate_names=["Petermann Bédat"],
        )
