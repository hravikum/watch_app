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
