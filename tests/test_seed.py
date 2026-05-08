"""Tests for the known-makers seed loader."""
from __future__ import annotations

from watch_app.extraction.seed import load_seed_makers
from watch_app.storage.models import Maker


def test_seed_loads_makers(session):
    stats = load_seed_makers(session)
    session.commit()

    # We should have created makers (not updated — DB was empty).
    assert stats["created"] > 80, f"expected ~100 makers, got {stats['created']}"
    # With a clean YAML (no duplicate canonical_names), no updates on first load.
    assert stats["updated"] == 0, (
        f"Got {stats['updated']} updates on first load — likely means the "
        f"YAML has duplicate canonical_name entries."
    )

    # Spot-check a few we know should be there.
    simon = session.query(Maker).filter_by(canonical_name="Simon Brette").one()
    assert simon.country == "France"
    assert simon.is_independent is True

    rolex = session.query(Maker).filter_by(canonical_name="Rolex").one()
    assert rolex.is_independent is False


def test_seed_is_idempotent(session):
    load_seed_makers(session)
    session.commit()
    first_count = session.query(Maker).count()

    stats2 = load_seed_makers(session)
    session.commit()
    second_count = session.query(Maker).count()

    assert first_count == second_count
    assert stats2["created"] == 0
    assert stats2["updated"] > 0


def test_seed_merges_aliases_without_losing_learned_ones(session):
    load_seed_makers(session)
    session.commit()

    # Pretend the extractor learned a new alias during a prior scan.
    raul = session.query(Maker).filter_by(canonical_name="Raúl Pagès").one()
    raul.aliases = [*(raul.aliases or []), "Raúl Pagés Genève"]
    session.commit()

    # Reload seed; the learned alias should be preserved, YAML aliases still present.
    load_seed_makers(session)
    session.commit()

    raul = session.query(Maker).filter_by(canonical_name="Raúl Pagès").one()
    assert "Raul Pages" in raul.aliases  # from YAML
    assert "Raúl Pagés Genève" in raul.aliases  # learned


def test_seed_does_not_delete_non_seed_makers(session):
    """A maker added by a source shouldn't vanish when seed runs again."""
    from watch_app.extraction.resolver import resolve_or_create_maker

    load_seed_makers(session)
    session.commit()

    # Simulate an article mentioning an unknown maker.
    new_maker = resolve_or_create_maker(session, "Totally New Atelier 2026")
    session.commit()
    new_id = new_maker.id

    load_seed_makers(session)
    session.commit()

    still_there = session.get(Maker, new_id)
    assert still_there is not None
    assert still_there.canonical_name == "Totally New Atelier 2026"


def test_seed_loads_website(session):
    """The seed loader should populate the website field from YAML on
    initial creation."""
    load_seed_makers(session)
    session.commit()

    journe = session.query(Maker).filter_by(canonical_name="F.P. Journe").one()
    assert journe.website == "https://www.fpjourne.com"


def test_seed_backfills_website_on_existing_maker(session):
    """A pre-existing maker without a website should get one written on
    the next seed load. This is the migration path for users whose DB
    was created before website data was added to the YAML."""
    # Manually create a maker without a website, simulating the pre-2M state.
    pre_existing = Maker(
        canonical_name="F.P. Journe",
        aliases=["FP Journe"],
        country="Switzerland",
        founded_year=1999,
        is_independent=True,
        website=None,
    )
    session.add(pre_existing)
    session.commit()

    # Run the seed loader. It should fill in website from YAML.
    load_seed_makers(session)
    session.commit()

    refreshed = (
        session.query(Maker).filter_by(canonical_name="F.P. Journe").one()
    )
    assert refreshed.website == "https://www.fpjourne.com"


def test_seed_does_not_overwrite_existing_website(session):
    """If a maker already has a website (e.g., set manually or by some
    future feature), the seed loader should NOT overwrite it. This
    matches how country and founded_year are handled."""
    pre_existing = Maker(
        canonical_name="F.P. Journe",
        aliases=["FP Journe"],
        country="Switzerland",
        founded_year=1999,
        is_independent=True,
        website="https://custom-override.example.com",
    )
    session.add(pre_existing)
    session.commit()

    load_seed_makers(session)
    session.commit()

    refreshed = (
        session.query(Maker).filter_by(canonical_name="F.P. Journe").one()
    )
    assert refreshed.website == "https://custom-override.example.com"


def test_seed_handles_makers_without_website(session):
    """Makers in the YAML without a website field should have website=None,
    not crash. Some lesser-known indies legitimately lack a public site."""
    load_seed_makers(session)
    session.commit()

    # Philippe Dufour is famously website-less; we leave him unmapped.
    dufour = (
        session.query(Maker).filter_by(canonical_name="Philippe Dufour").one()
    )
    assert dufour.website is None
