"""Seed YAML audit tests.

These tests guard against the recurring failure mode where a conglomerate
brand gets added to the seed YAML (or stays in the DB after auto-creation)
with `is_independent: True`, causing it to pollute the rising_star ranking.

The test-before-merge pattern: any time you find a conglomerate in the top
20 of rising_star, add it to CONGLOMERATE_BRANDS below. This file fails
CI if any of those brands are missing from the seed or marked as independent.
"""
from __future__ import annotations

from watch_app.extraction.seed import load_seed_makers
from watch_app.storage.models import Maker


#: Brands the LV Watch Prize would never consider "independent creative"
#: — large corporate-group-owned, widely distributed, appearing in GPHG as
#: entrants rather than discovery-worthy indies. Each MUST be in the seed
#: YAML with is_independent=False.
CONGLOMERATE_BRANDS = {
    # Richemont
    "Cartier",
    "IWC",
    "Jaeger-LeCoultre",
    "Panerai",
    "Vacheron Constantin",
    "Piaget",
    "Van Cleef & Arpels",
    # LVMH
    "Louis Vuitton",
    "Tiffany & Co.",
    "Hublot",
    "Chaumet",
    "Dior Montres",
    "Bvlgari",
    "Tudor",  # wait — Tudor is Rolex SA, not LVMH; kept because non-indie
    "Zenith",
    # Swatch Group
    "Breguet",
    "Blancpain",
    "Omega",
    # Kering
    "Gucci",
    # Independently-listed conglomerates and group-owned brands
    "Rolex",
    "Patek Philippe",  # Stern-family-owned but corp scale; non-indie by LV-Prize lens
    "Audemars Piguet",
    "Chopard",
    "Grand Seiko",  # Seiko / Citizen-comparable scale
    "Credor",
    "Hermès",
    "Chanel",
    "Breitling",
    "Fabergé",
    "Frédérique Constant",
    "Raymond Weil",
    "Franck Muller",
    "Gérald Genta",
    "Daniel Roth",
}


def test_all_known_conglomerates_are_in_seed_as_non_independent(session):
    """Core invariant: every brand in CONGLOMERATE_BRANDS must be seeded with
    is_independent=False. This test catches regressions where:

    1. A conglomerate was mistakenly added to the seed as independent
    2. A conglomerate was added to the seed without the is_independent flag
       (which defaults to True)
    3. A conglomerate surfaced through GPHG auto-create but never got promoted
       to the seed

    When adding a new brand to CONGLOMERATE_BRANDS, also add it to
    known_makers.yaml — this test will remind you.
    """
    load_seed_makers(session)
    session.commit()

    missing = []
    wrong = []
    for brand in CONGLOMERATE_BRANDS:
        m = (
            session.query(Maker)
            .filter(Maker.canonical_name == brand)
            .one_or_none()
        )
        if m is None:
            missing.append(brand)
        elif m.is_independent:
            wrong.append(brand)

    assert not missing, (
        f"Missing from seed YAML: {missing}. Add entries to "
        f"watch_app/data/known_makers.yaml with is_independent: false."
    )
    assert not wrong, (
        f"In seed but marked is_independent=True: {wrong}. Correct them "
        f"to is_independent: false in watch_app/data/known_makers.yaml."
    )
