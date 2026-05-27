"""Subject-of-article LLM extraction trigger (2U).

The rule-based extractor in `entities.py` matches any seed-known maker
name appearing in the article title or body. That works well when the
article is about a known maker — but fails when the article is about
a new (unknown) indie maker that's also being described in terms of
established references.

Example: "Introducing: The Mermont La Parfaite Considers The Simple
Art Of Time Telling In Elevated Form (Live Pics)" on Hodinkee. The
body mentions "Breguet-style numerals" and "La Joux-Perret D101". The
rule-based extractor matches "Breguet" and attributes the mention to
Breguet. The actual subject — Mermont — is invisible because it's
not in the seed vocabulary.

This module adds a targeted LLM extraction pass that fires only in
the cases where the rule-based extractor most likely got it wrong:

1. The article title starts with a journalism convention marker
   suggesting it's announcing or featuring a single subject:
   "Introducing:", "Hands On:", "Hands-On:", "First Look:", "In Depth:",
   "Meet", or variants with em-dashes
2. AND the rule-based extractor attributed the mention exclusively
   to one or more "common reference" makers (Rolex, Patek, etc.) —
   the brands constantly invoked in comparison without being the
   actual subject

When both conditions match, we call the existing LLM entity extraction
to identify the article's true subject. The LLM's findings REPLACE the
common-reference attributions (not supplement them) — attributing
"Breguet-style numerals" articles to Breguet inflates Breguet's mention
count for articles that aren't really about it.

The trigger is conservative by design: false positives (calling LLM
on articles where the rule-based attribution was actually correct)
cost ~$0.003 per article. False negatives (missing genuine subject
attribution) are the problem we're trying to solve.

For cases outside this trigger — articles attributed to non-reference
makers (e.g., Voutilainen, Akrivia) — we trust the rule-based result.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


#: Canonical names treated as "common reference" — brands universally
#: invoked as comparison points but rarely the actual subject of indie-
#: debut articles. Membership here is a gate, not a permanent label —
#: edit this list to tune.
#:
#: Deliberately excludes brands that are both reference points AND
#: frequent indie subjects (e.g., Gerald Genta, F.P. Journe). Better
#: to occasionally call LLM unnecessarily than to skip a legitimate
#: F.P. Journe article.
COMMON_REFERENCE_MAKERS = frozenset(
    [
        "Rolex",
        "Patek Philippe",
        "Omega",
        "Breguet",
        "Audemars Piguet",
        "IWC",
        "Cartier",
        "Vacheron Constantin",
        "Jaeger-LeCoultre",
        "Breitling",
        "Tudor",
        "A. Lange & Söhne",
        "Blancpain",
    ]
)


#: Title-prefix patterns suggesting the article is about a single
#: subject. These are the journalism conventions across the sources
#: we ingest (Hodinkee, SJX, Monochrome, Fratello, Quill & Pad).
#:
#: Patterns are matched case-insensitively against the start of the
#: title, after stripping leading whitespace. Em-dashes and colons
#: are both common; we handle both.
SUBJECT_PREFIX_PATTERNS = [
    re.compile(r"^\s*introducing\s*[:\u2013\-]", re.IGNORECASE),
    re.compile(r"^\s*hands[\s\-]*on\s*[:\u2013\-]", re.IGNORECASE),
    re.compile(r"^\s*first\s*look\s*[:\u2013\-]", re.IGNORECASE),
    re.compile(r"^\s*in\s*depth\s*[:\u2013\-]", re.IGNORECASE),
    re.compile(r"^\s*meet\s+", re.IGNORECASE),
]


def title_suggests_single_subject(title: str | None) -> bool:
    """Does the title use a journalism convention indicating a single subject?

    Examples that match:
      - "Introducing: The Mermont La Parfaite..."
      - "Hands On: Ressence Type 11"
      - "Hands-On With The Tudor Monarch..."
      - "First Look – The New Steel Editions..."
      - "In Depth: Leroy 01"
      - "Meet the New Watchmaker..."

    Examples that don't:
      - "RJ's Best 5 Watches From Watches And Wonders 2026"
      - "Watches And Wonders 2026 Roundup"
      - "SJX Podcast: Geneva Auction Preview"
      - "Bring a Loupe: ..." (multi-watch auction roundup)
    """
    if not title:
        return False
    return any(p.match(title) for p in SUBJECT_PREFIX_PATTERNS)


def all_attributions_are_common_references(
    candidate_names: Iterable[str],
) -> bool:
    """True if every attributed name is in COMMON_REFERENCE_MAKERS.

    If the list is empty, returns False (no attributions means no
    common-reference gate triggers — the existing "no candidates"
    path in the orchestrator handles that case).
    """
    names = list(candidate_names)
    if not names:
        return False
    return all(name in COMMON_REFERENCE_MAKERS for name in names)


def should_attempt_subject_extraction(
    title: str | None,
    candidate_names: Iterable[str],
) -> bool:
    """Decide whether to invoke LLM subject extraction on this mention.

    Returns True only if BOTH conditions are met:
      1. The title looks like it's announcing a single subject
      2. The rule-based extractor attributed only to common-reference
         makers (or those plus nothing else)

    Conservative by design — false positives cost ~$0.003 each.
    """
    if not title_suggests_single_subject(title):
        return False
    return all_attributions_are_common_references(candidate_names)


def extract_article_subject(
    session: Session,
    title: str,
    body: str,
) -> list[str]:
    """Call LLM to identify the actual subject of an article.

    Returns AT MOST ONE maker name — the LLM's highest-confidence
    indie identification. We cap at 1 because:

    1. The trigger fires on "Introducing/Hands On/First Look/In Depth"
       articles, which by convention have a single subject.
    2. The LLM's general extraction can return secondary references
       (co-watchmakers, founders, historical inspirations) that aren't
       separate brands. Treating those as new Maker rows pollutes
       the discovery view with non-brand entities.

    Examples of secondaries we DON'T want to commit as separate makers:
    - "Nicolas Delaloye" (Mermont's watchmaker — a person, not a brand)
    - "Sebastien Bey-Haut" (Mermont's founder)
    - "Louis Audemars", "John Arnold" (historical inspirations in a
      Victor Kullberg article)

    Cost of being conservative: we occasionally miss a legitimate co-
    attribution (e.g., a genuinely collaborative watch). That's worth
    it; manual curation can add such cases as aliases or separate seed
    entries.

    Reuses the existing `extract_makers` LLM call from chunk 2N —
    same cache, same prompt, same confidence threshold. Filters to
    independent makers (we're looking for indie subjects).
    """
    from watch_app.extraction.entities_llm import MIN_CONFIDENCE
    from watch_app.llm.client import extract_makers

    try:
        result = extract_makers(session=session, title=title, text=body)
    except Exception as exc:
        # Log both the wrapped exception AND its underlying cause so we can
        # actually diagnose what's failing. The Anthropic SDK wraps low-level
        # errors (DNS, TLS, connection refused, etc.) into APIConnectionError
        # with a generic "Connection error." message — the actual error type
        # is only visible via exc.__cause__ (2W-llm-diag).
        logger.warning(
            "Subject extraction LLM call failed for title=%r: %s (cause: %r)",
            title[:80], exc, exc.__cause__,
        )
        return []

    accepted = [
        m.name
        for m in result.makers
        if m.confidence >= MIN_CONFIDENCE and m.is_independent
    ]
    # Cap at the first/highest-confidence result. The LLM returns makers
    # in confidence order, so [0] is the primary subject.
    return accepted[:1]
