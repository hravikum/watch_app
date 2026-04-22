"""LLM-backed entity extraction.

Called from the pipeline when `--use-llm` is set. Takes a RawMention whose
`candidate_names` is empty and populates it using Claude.

Sources that already know the maker (awards, explicit listings) don't go
through this path — their RawMention already has candidate_names set.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from watch_app.llm.client import extract_makers
from watch_app.sources.base import RawMention


logger = logging.getLogger(__name__)

#: Confidence threshold for accepting an LLM-extracted name.
MIN_CONFIDENCE = 0.5


def populate_candidates_from_llm(
    session: Session,
    mention: RawMention,
) -> None:
    """Mutates `mention.candidate_names` in place using LLM extraction.

    If the mention already has candidate names, it's a no-op (we trust what
    the source gave us).
    """
    if mention.candidate_names:
        return

    title = mention.title or ""
    body = mention.raw_text or mention.excerpt or ""
    if not body and not title:
        return

    try:
        result = extract_makers(session=session, title=title, text=body)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("LLM extraction failed for %r: %s", mention.url, exc)
        return

    accepted = [
        m.name
        for m in result.makers
        if m.confidence >= MIN_CONFIDENCE and m.is_independent
    ]
    mention.candidate_names = accepted
