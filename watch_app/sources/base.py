"""Base classes for sources.

A `Source` knows how to fetch from somewhere and yield `RawMention` objects.
The pipeline then extracts/resolves maker names from those mentions and writes
persistent `Mention` rows to the DB.

Key separation of concerns:
- `RawMention` (here) = what a source produces. Has candidate text and metadata
  but doesn't know about DB IDs or canonical Maker identity.
- `Mention` (in storage/models.py) = what ends up in the DB, with maker_id
  already resolved.

This lets sources be pure data producers with no DB dependency.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator


@dataclass
class RawMention:
    """A candidate mention produced by a source, before extraction/resolution.

    `candidate_names` is populated by sources that already know who the article
    is about (e.g. awards lists where the maker name is explicit). For media
    sources, this starts empty and the extraction layer fills it in.
    """

    source_name: str
    url: str
    title: str | None = None
    excerpt: str | None = None
    raw_text: str | None = None
    published_at: datetime | None = None
    candidate_names: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"<RawMention source={self.source_name!r} url={self.url!r}>"


class Source(ABC):
    """Abstract base class for all sources."""

    #: Human-readable source name. Used as the unique key in the sources table.
    name: str

    #: Category: awards / media / community / social.
    category: str

    #: Prestige weight used in scoring. 0.0–1.0. Tune in config/scoring.yaml later.
    prestige_weight: float = 0.5

    @abstractmethod
    def fetch(self) -> Iterator[RawMention]:
        """Yield RawMention objects from this source.

        Implementations should be generators — we may be processing many
        mentions and don't want them all in memory at once.

        Networking, parsing, and error handling live here. A well-behaved source
        logs and skips on per-item errors rather than aborting the whole run.
        """
        ...
