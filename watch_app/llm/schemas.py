"""Pydantic schemas for structured outputs from the LLM.

Using Pydantic (which we already have for FastAPI) gives us:
- Validation that the LLM returned what we asked for.
- Type safety in the calling code.
- Easy serialization to/from JSON for the LLM cache.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractedMaker(BaseModel):
    """A single watchmaker mention extracted from article text."""

    name: str = Field(description="The maker or brand name as it appears in the text.")
    is_independent: bool = Field(
        default=True,
        description=(
            "True if this appears to be an independent watchmaker or small "
            "atelier; False for established luxury houses (Rolex, Patek, etc.)."
        ),
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Extractor confidence in this identification (0.0–1.0).",
    )


class ExtractionResult(BaseModel):
    """LLM response for the 'extract makers from article' prompt."""

    makers: list[ExtractedMaker] = Field(default_factory=list)
