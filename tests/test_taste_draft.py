"""Tests for the Layer 1b taste-drafting tool (chunk 3B).

The LLM call is mocked everywhere — these tests cover sanitization, target
selection, and the YAML writer, not the model.
"""
from __future__ import annotations

import yaml

from watch_app import taste
from watch_app.extraction import taste_draft
from watch_app.extraction.taste_draft import (
    TasteProposal,
    is_tagged,
    propose_taste,
    untagged_independents,
    write_drafts_to_yaml,
)


# ---- sanitize_taste ----


def test_sanitize_drops_unknown_and_coerces():
    raw = {
        "aesthetic_tags": ["classical", "steampunk", "classical"],  # junk + dup
        "complication_focus": "tourbillon",  # wrong type (str not list)
        "maker_type": "solo-watchmaker",
        "price_band": "cheap",  # not in vocab
        "output_scale": None,  # abstained
        "movement_origin": "in-house",
        "favourite_colour": "teal",  # not a taste field
    }
    clean = taste.sanitize_taste(raw)
    assert clean["aesthetic_tags"] == ["classical"]  # junk + dup removed
    assert "complication_focus" not in clean  # bad type dropped
    assert clean["maker_type"] == "solo-watchmaker"
    assert "price_band" not in clean  # out-of-vocab dropped
    assert "output_scale" not in clean
    assert clean["movement_origin"] == "in-house"
    assert "favourite_colour" not in clean
    # The cleaned dict must always validate.
    taste.validate_taste_fields({**clean, "canonical_name": "X"})


def test_sanitize_empty_when_all_invalid():
    assert taste.sanitize_taste({"price_band": "nope", "maker_type": 5}) == {}


# ---- target selection against the real seed ----


def test_untagged_independents_excludes_conglomerates_and_tagged():
    targets = untagged_independents()
    names = {e["canonical_name"] for e in targets}
    # Conglomerates are never targets.
    assert "Rolex" not in names
    # Already-tagged makers (1a hand-tags) are never targets.
    assert "F.P. Journe" not in names
    # Every returned entry is, structurally, an untagged independent — this
    # stays true no matter how many makers get tagged as 1b proceeds.
    for e in targets:
        assert e.get("is_independent", True) is True
        assert not is_tagged(e)


# ---- propose_taste sanitizes the LLM output ----


def test_propose_taste_sanitizes(session, monkeypatch):
    def fake_lookup(_session, name, country=None):
        return {
            "aesthetic_tags": ["classical", "bogus"],
            "maker_type": "small-atelier",
            "price_band": "high",
            "output_scale": "not-a-real-scale",
            "complication_focus": ["time-only"],
        }

    monkeypatch.setattr(taste_draft, "lookup_maker_taste", fake_lookup)

    prop = propose_taste(session, {"canonical_name": "Test Maker", "country": "France"})
    assert prop.canonical_name == "Test Maker"
    assert prop.tags["aesthetic_tags"] == ["classical"]
    assert prop.tags["maker_type"] == "small-atelier"
    assert "output_scale" not in prop.tags  # invalid → dropped


# ---- YAML writer ----


_MINI_YAML = """\
makers:
  - canonical_name: "Alpha Maker"
    country: "France"
    is_independent: true
  - canonical_name: "Beta Maker"
    country: "Japan"
    is_independent: true
"""


def test_write_drafts_inserts_valid_block(tmp_path):
    path = tmp_path / "mini.yaml"
    path.write_text(_MINI_YAML, encoding="utf-8")

    proposals = [
        TasteProposal(
            canonical_name="Alpha Maker",
            tags={
                "aesthetic_tags": ["classical"],
                "maker_type": "solo-watchmaker",
                "complication_focus": ["time-only"],
            },
        ),
        TasteProposal(canonical_name="Beta Maker", tags={}),  # abstained — skip
    ]
    written = write_drafts_to_yaml(proposals, path=path)
    assert written == 1

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    by_name = {e["canonical_name"]: e for e in data["makers"]}

    # Alpha got tags, in vocabulary, and the original fields are intact.
    alpha = by_name["Alpha Maker"]
    assert alpha["aesthetic_tags"] == ["classical"]
    assert alpha["maker_type"] == "solo-watchmaker"
    assert alpha["country"] == "France"
    taste.validate_taste_fields(alpha)

    # Beta untouched (no taste fields).
    beta = by_name["Beta Maker"]
    assert all(beta.get(f) is None for f in taste.TASTE_FIELDS)

    # No duplicate entries introduced.
    names = [e["canonical_name"] for e in data["makers"]]
    assert len(names) == len(set(names)) == 2


def test_write_drafts_noop_when_all_empty(tmp_path):
    path = tmp_path / "mini.yaml"
    path.write_text(_MINI_YAML, encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    written = write_drafts_to_yaml(
        [TasteProposal(canonical_name="Alpha Maker", tags={})], path=path
    )
    assert written == 0
    assert path.read_text(encoding="utf-8") == before  # file untouched
