"""Tests for subject-extraction trigger (2U).

Verifies:
  1. Title prefix detection (Introducing, Hands On, etc.)
  2. Common-reference check
  3. Combined trigger logic
  4. LLM extraction adapter returns expected shape
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from watch_app.extraction.subject import (
    COMMON_REFERENCE_MAKERS,
    all_attributions_are_common_references,
    extract_article_subject,
    should_attempt_subject_extraction,
    title_suggests_single_subject,
)


# ---------- Title prefix detection ----------


class TestTitleSuggestsSingleSubject:
    """Real titles from the diagnostic should be classified correctly."""

    def test_introducing_colon(self):
        assert title_suggests_single_subject(
            "Introducing: The Mermont La Parfaite Considers The Simple Art..."
        )

    def test_introducing_em_dash(self):
        # Monochrome uses em-dashes
        assert title_suggests_single_subject(
            "Introducing – Naoya Hida Presents its 2026 Collection"
        )

    def test_hands_on_colon(self):
        assert title_suggests_single_subject("Hands On: Ressence Type 11")

    def test_hands_on_hyphenated(self):
        assert title_suggests_single_subject(
            "Hands-On: The Vacheron Constantin Overseas Ultra-Thin 2500V"
        )

    def test_hands_on_with(self):
        # "Hands-On With" — variant where the colon is replaced with "With"
        # Our pattern requires a punctuation marker after hands-on so this
        # is intentionally NOT matched. This is conservative; we'd rather
        # miss a few than over-trigger LLM calls.
        assert not title_suggests_single_subject(
            "Hands-On With The Tudor Monarch — Are You Down With The New King?"
        )

    def test_first_look(self):
        assert title_suggests_single_subject(
            "First Look – The New Steel Editions of the Haute-Rive Honoris"
        )

    def test_in_depth(self):
        assert title_suggests_single_subject("In Depth: Leroy 01")

    def test_meet(self):
        assert title_suggests_single_subject(
            "Meet the New Watchmaker on the Scene"
        )

    def test_doesnt_match_recap(self):
        # Recap articles legitimately cover multiple brands; we don't trigger.
        assert not title_suggests_single_subject(
            "Recap – The Best New Indie Watches From Geneva Watch Week 2026"
        )

    def test_doesnt_match_podcast(self):
        assert not title_suggests_single_subject(
            "SJX Podcast: Geneva Auction Preview"
        )

    def test_doesnt_match_roundup(self):
        assert not title_suggests_single_subject(
            "Watches And Wonders 2026 Roundup — Gerald Charles"
        )

    def test_doesnt_match_top5(self):
        assert not title_suggests_single_subject(
            "Fratello's Top 5 Tudor Black Bay Alternatives In 2026"
        )

    def test_doesnt_match_bring_a_loupe(self):
        # Multi-watch auction roundups — should NOT trigger LLM
        assert not title_suggests_single_subject(
            "Bring a Loupe: A White-Gold Vacheron Constantin, An IWC..."
        )

    def test_none_title(self):
        assert not title_suggests_single_subject(None)

    def test_empty_title(self):
        assert not title_suggests_single_subject("")

    def test_case_insensitive(self):
        assert title_suggests_single_subject("INTRODUCING: A New Brand")
        assert title_suggests_single_subject("introducing: a new brand")


# ---------- Common-reference attribution check ----------


class TestAllAttributionsAreCommonReferences:
    def test_single_reference_maker(self):
        assert all_attributions_are_common_references(["Breguet"])

    def test_multiple_reference_makers(self):
        assert all_attributions_are_common_references(["Rolex", "Patek Philippe"])

    def test_includes_non_reference_maker(self):
        # If even one attribution is to a non-reference maker, we trust
        # the rule-based result and don't trigger LLM.
        assert not all_attributions_are_common_references(
            ["Breguet", "Voutilainen"]
        )

    def test_only_non_reference(self):
        assert not all_attributions_are_common_references(["Voutilainen"])

    def test_empty(self):
        # No attributions means no common-reference gate triggers.
        assert not all_attributions_are_common_references([])


# ---------- Combined trigger ----------


class TestShouldAttemptSubjectExtraction:
    def test_mermont_case(self):
        """The textbook trigger case from the diagnostic."""
        assert should_attempt_subject_extraction(
            "Introducing: The Mermont La Parfaite Considers The Simple Art...",
            ["Breguet"],
        )

    def test_naoya_hida_case(self):
        """Monochrome em-dash variant attributed to other indies."""
        # Naoya Hida's article was attributed to multiple makers including
        # Hajime Asaoka and Kurono — both non-reference. So we should NOT
        # trigger LLM (trust the rule-based result).
        assert not should_attempt_subject_extraction(
            "Introducing – Naoya Hida Presents its 2026 Collection",
            ["Hajime Asaoka", "Kurono Tokyo"],
        )

    def test_legitimate_breguet_article(self):
        """An actual Breguet article shouldn't trigger LLM — even though
        the attribution is reference-only, the title doesn't suggest a
        different subject."""
        assert not should_attempt_subject_extraction(
            "Breguet Releases New Classique Souscription",  # no "Introducing:"
            ["Breguet"],
        )

    def test_introducing_breguet_article(self):
        """If the title DOES start with 'Introducing:' AND Breguet is in
        the title, the rule-based extractor would attribute correctly.
        But our trigger logic doesn't know that — title-prefix and common-
        reference both match. We'll call LLM unnecessarily; LLM should
        return Breguet (confirming) and we'll replace Breguet with...
        Breguet. No harm done, but worth noting the false positive."""
        # We don't test the harm here — just that the trigger fires.
        assert should_attempt_subject_extraction(
            "Introducing: The New Breguet Classique Souscription",
            ["Breguet"],
        )

    def test_no_attribution(self):
        """If no rule-based attribution happened, the existing 'no
        candidates' path handles it; this trigger doesn't fire."""
        assert not should_attempt_subject_extraction(
            "Introducing: The Mermont La Parfaite",
            [],
        )

    def test_non_subject_title(self):
        """Recap and roundup articles shouldn't trigger even if attributed
        only to reference makers."""
        assert not should_attempt_subject_extraction(
            "Watches And Wonders 2026 Roundup",
            ["Rolex", "Patek Philippe"],
        )


# ---------- LLM extraction adapter ----------


class TestExtractArticleSubject:
    def test_filters_by_confidence(self, test_engine):
        """Only makers above MIN_CONFIDENCE should be returned."""
        from watch_app.storage.db import session_scope

        mock_result = MagicMock()
        mock_result.makers = [
            MagicMock(name="Confident", confidence=0.9, is_independent=True),
            MagicMock(name="Unsure", confidence=0.3, is_independent=True),
        ]
        # Configure name property (MagicMock's `name` is reserved)
        mock_result.makers[0].name = "Mermont"
        mock_result.makers[1].name = "Maybe Brand"

        with session_scope() as session, \
             patch("watch_app.llm.client.extract_makers", return_value=mock_result):
            result = extract_article_subject(session, "Title", "Body")

        assert result == ["Mermont"]

    def test_filters_by_independence(self, test_engine):
        """Conglomerate-flagged makers should be filtered out (we're
        looking for indie subjects)."""
        from watch_app.storage.db import session_scope

        mock_result = MagicMock()
        m1 = MagicMock(confidence=0.9, is_independent=True)
        m1.name = "Mermont"
        m2 = MagicMock(confidence=0.9, is_independent=False)
        m2.name = "Rolex"
        mock_result.makers = [m1, m2]

        with session_scope() as session, \
             patch("watch_app.llm.client.extract_makers", return_value=mock_result):
            result = extract_article_subject(session, "Title", "Body")

        assert "Rolex" not in result
        assert "Mermont" in result

    def test_handles_exception(self, test_engine):
        """LLM API errors return empty list, not raise."""
        from watch_app.storage.db import session_scope

        with session_scope() as session, \
             patch("watch_app.llm.client.extract_makers",
                   side_effect=ConnectionError("API down")):
            result = extract_article_subject(session, "Title", "Body")

        assert result == []

    def test_caps_at_one_subject(self, test_engine):
        """Even when LLM returns multiple confident indie subjects, we
        return only the first (highest-confidence) one. Secondary
        returns are usually founders / watchmakers / historical refs
        that shouldn't become separate Maker rows (2U-hotfix)."""
        from watch_app.storage.db import session_scope

        mock_result = MagicMock()
        # Three confident, independent makers — the LLM's general
        # extraction returns subject + founder + historical reference.
        primary = MagicMock(confidence=0.95, is_independent=True)
        primary.name = "Mermont"
        secondary = MagicMock(confidence=0.85, is_independent=True)
        secondary.name = "Nicolas Delaloye"  # The watchmaker, not a brand
        tertiary = MagicMock(confidence=0.75, is_independent=True)
        tertiary.name = "Sebastien Bey-Haut"  # The founder, not a brand
        mock_result.makers = [primary, secondary, tertiary]

        with session_scope() as session, \
             patch("watch_app.llm.client.extract_makers", return_value=mock_result):
            result = extract_article_subject(session, "Title", "Body")

        # Only the primary subject returned.
        assert result == ["Mermont"]


# ---------- Common reference list sanity ----------


def test_common_references_includes_top_offenders():
    """The list should at minimum include the top offenders from the
    diagnostic run (Rolex, Patek Philippe, Omega, Breguet)."""
    assert "Rolex" in COMMON_REFERENCE_MAKERS
    assert "Patek Philippe" in COMMON_REFERENCE_MAKERS
    assert "Omega" in COMMON_REFERENCE_MAKERS
    assert "Breguet" in COMMON_REFERENCE_MAKERS


def test_common_references_excludes_indies_that_are_subjects():
    """Indie makers that genuinely have articles written about them
    shouldn't be in the reference list — even if they're occasionally
    mentioned as influences. F.P. Journe and Gerald Genta were both
    in the top-10 of the diagnostic but should NOT gate LLM extraction
    because they're frequently the actual subject."""
    assert "F.P. Journe" not in COMMON_REFERENCE_MAKERS
    assert "Gerald Genta" not in COMMON_REFERENCE_MAKERS
    assert "Voutilainen" not in COMMON_REFERENCE_MAKERS
