"""Versioned prompt templates.

Prompts are versioned because the LLM cache key includes the prompt version —
changing a prompt should invalidate cached responses that were generated under
the old one.

Bump the version string when you meaningfully change a prompt's expected output.
"""
from __future__ import annotations


EXTRACT_MAKERS_PROMPT_VERSION = "v1"

EXTRACT_MAKERS_SYSTEM = """You extract names of watchmakers and watch brands from article text.

Focus on INDEPENDENT watchmakers and small ateliers (the kind that might be
nominated for the Louis Vuitton Watch Prize for Independent Creatives, GPHG,
or Dubai Watch Week showcases). Large established luxury houses like Rolex,
Patek Philippe, Audemars Piguet, Omega, Cartier, TAG Heuer, and similar
conglomerate-owned brands should be marked is_independent=false — we track
them but weight them down in scoring.

Return ONLY a JSON object matching this schema, no prose before or after:

{
  "makers": [
    {"name": "string", "is_independent": true|false, "confidence": 0.0-1.0}
  ]
}

If no watchmakers are mentioned, return {"makers": []}.
"""

EXTRACT_MAKERS_USER_TEMPLATE = """Article title: {title}

Article text:
{text}
"""


def format_extract_prompt(title: str, text: str) -> tuple[str, str]:
    """Return (system, user) prompt strings for the extraction task."""
    return (
        EXTRACT_MAKERS_SYSTEM,
        EXTRACT_MAKERS_USER_TEMPLATE.format(title=title or "(no title)", text=text),
    )
