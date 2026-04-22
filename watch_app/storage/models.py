"""SQLAlchemy models — the core data model of the app.

Design notes:
- Mentions are immutable facts. Signals and Scores are derived and can be
  recomputed from Mentions without re-scraping.
- `aliases` on Maker is a JSON list of alternate names used for matching.
- Historical Signals and Scores are kept (not overwritten) so we can show
  trends over time.
- LLMCache is content-hash-keyed; the same article is never processed twice.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Shared declarative base."""

    pass


class Maker(Base):
    """A watchmaker, brand, or atelier. The canonical entity."""

    __tablename__ = "makers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)

    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    founded_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instagram_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    youtube_channel: Mapped[str | None] = mapped_column(String(200), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_independent: Mapped[bool] = mapped_column(Boolean, default=True)
    production_volume_estimate: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    mentions: Mapped[list["Mention"]] = relationship(back_populates="maker")
    signals: Mapped[list["Signal"]] = relationship(back_populates="maker")
    scores: Mapped[list["Score"]] = relationship(back_populates="maker")

    def __repr__(self) -> str:
        return f"<Maker {self.canonical_name!r}>"


class Source(Base):
    """A place we pull mentions from.

    Prestige weight is a per-source multiplier used in scoring. Monochrome or SJX
    might be 1.0; a random blog might be 0.3. Tuned in config/scoring.yaml later.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(50))  # awards/media/community/social
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    prestige_weight: Mapped[float] = mapped_column(Float, default=0.5)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    mentions: Mapped[list["Mention"]] = relationship(back_populates="source")

    def __repr__(self) -> str:
        return f"<Source {self.name!r}>"


class Mention(Base):
    """A single reference to a Maker found at a Source.

    Mentions are immutable. If we re-scrape an article, we upsert by (source_id, url).
    """

    __tablename__ = "mentions"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "url", "maker_id", name="uq_mention_source_url_maker"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    maker_id: Mapped[int] = mapped_column(ForeignKey("makers.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)

    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    maker: Mapped["Maker"] = relationship(back_populates="mentions")
    source: Mapped["Source"] = relationship(back_populates="mentions")

    def __repr__(self) -> str:
        return f"<Mention maker_id={self.maker_id} source_id={self.source_id}>"


class Signal(Base):
    """A computed value about a Maker at a point in time.

    Examples: mention_count_90d=7, source_diversity=4, first_seen_days_ago=120.
    Kept historically — don't overwrite; insert new rows.
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    maker_id: Mapped[int] = mapped_column(ForeignKey("makers.id"), index=True)
    signal_type: Mapped[str] = mapped_column(String(100), index=True)
    value: Mapped[float] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    maker: Mapped["Maker"] = relationship(back_populates="signals")

    def __repr__(self) -> str:
        return f"<Signal {self.signal_type}={self.value} for maker_id={self.maker_id}>"


class Score(Base):
    """A composite ranking for a Maker under a given preset.

    Historical — one row per (maker, preset, computed_at). The UI usually shows
    the latest score per preset, but the history enables trend views.
    """

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    maker_id: Mapped[int] = mapped_column(ForeignKey("makers.id"), index=True)
    preset: Mapped[str] = mapped_column(String(50), index=True)
    value: Mapped[float] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    maker: Mapped["Maker"] = relationship(back_populates="scores")

    def __repr__(self) -> str:
        return (
            f"<Score maker_id={self.maker_id} preset={self.preset!r} "
            f"value={self.value:.3f}>"
        )


class LLMCache(Base):
    """Content-hash keyed cache for LLM responses.

    Prevents paying twice for the same prompt+input. Key is a SHA256 of
    (model, prompt_version, input_content).
    """

    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50))
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<LLMCache key={self.cache_key[:8]}... model={self.model}>"
