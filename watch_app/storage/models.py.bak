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

    # Enrichment tracking — populated when LLM-driven enrichment fills in
    # missing fields. `enriched_at` is the most recent enrichment timestamp;
    # `enriched_fields` is the list of field names that were LLM-enriched.
    # Fields populated via seed YAML or manual SQL stay None here.
    #
    # See chunk 2S for context. Currently set only by `cli enrich` (2Q); chunk
    # 2T will also set them from the automatic-on-scan enrichment path.
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enriched_fields: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # Taste / character dimensions — curated, used by the maker profile panel
    # and (later) the multi-dimension explorer. All nullable: an untagged
    # maker simply renders no profile. The controlled vocabulary lives in
    # watch_app/taste.py; the seed loader validates against it. See chunk 3A.
    aesthetic_tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    complication_focus: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    maker_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    price_band: Mapped[str | None] = mapped_column(String(20), nullable=True)
    output_scale: Mapped[str | None] = mapped_column(String(20), nullable=True)
    distribution: Mapped[str | None] = mapped_column(String(30), nullable=True)
    movement_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Set once the taste auto-tagger has attempted this maker (chunk 3F),
    # whether or not it produced any tags. It's the once-per-maker gate for
    # automatic taste tagging of auto-discovered makers — distinct from
    # `enriched_at`, which tracks the factual on-scan enrichment (website /
    # country / is_independent). Stays None for seed makers (their taste is
    # curated in the YAML) and for any maker the tagger hasn't reached yet.
    taste_enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    mentions: Mapped[list["Mention"]] = relationship(back_populates="maker")
    signals: Mapped[list["Signal"]] = relationship(back_populates="maker")
    scores: Mapped[list["Score"]] = relationship(back_populates="maker")
    watch_models: Mapped[list["WatchModel"]] = relationship(back_populates="maker")

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


class WatchModel(Base):
    """A specific watch model from a maker.

    Sourced from GPHG nominee pages: each watch nominated for any GPHG edition
    becomes one WatchModel row. Bounded scope by design — we don't try to
    catalog everything a maker produces, only watches that have been GPHG-
    recognized. This keeps the data quality high (every row has a real source
    URL and verified specs) without taking on the maintenance burden of
    per-brand-website scrapers.

    The unique constraint on (maker_id, gphg_url) means re-fetching a watch
    page on a later scan updates the row in place — useful if GPHG corrects
    a spec or adds a price.
    """

    __tablename__ = "watch_models"
    __table_args__ = (UniqueConstraint("gphg_url", name="uq_watch_models_gphg_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    maker_id: Mapped[int] = mapped_column(
        ForeignKey("makers.id"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(300))
    slug: Mapped[str] = mapped_column(String(200))  # last path segment of gphg_url
    gphg_year: Mapped[int] = mapped_column(Integer, index=True)
    gphg_url: Mapped[str] = mapped_column(String(500))

    # Spec fields — all nullable because GPHG data isn't always complete.
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    case_material: Mapped[str | None] = mapped_column(String(200), nullable=True)
    case_diameter_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    movement_caliber: Mapped[str | None] = mapped_column(String(200), nullable=True)
    movement_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    frequency_hz: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_reserve_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    water_resistance_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    complications: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    price_chf: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limited_edition_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    maker: Mapped["Maker"] = relationship(back_populates="watch_models")

    def __repr__(self) -> str:
        return f"<WatchModel {self.name!r} ({self.gphg_year})>"


class ScanRun(Base):
    """Record of one invocation of run_scan().

    Every scan (manual or scheduled) creates exactly one ScanRun row. The row
    is created at scan start with status='running', then updated to 'completed'
    or 'failed' when the scan ends. If the scan process crashes hard (power
    loss, kill -9), the row may be left in 'running' indefinitely — that's a
    useful signal when reviewing history.

    We store counts (sources_run, mentions_written, makers_scored) so the
    `history` CLI can show trends at a glance.
    """

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    # Counters — nullable so they're empty while status='running'
    sources_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mentions_written: Mapped[int | None] = mapped_column(Integer, nullable=True)
    makers_scored: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How the scan was invoked ('manual', 'scheduled', or 'test')
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    # Populated only on failure
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ScanRun id={self.id} started={self.started_at} "
            f"status={self.status!r}>"
        )

    @property
    def duration_seconds(self) -> float | None:
        """Return run duration in seconds, or None if still running."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


# 2V-cleanup: a dead ``Model`` class previously lived here (table ``models``).
# It was a leftover from an earlier design that got replaced by ``WatchModel``
# (table ``watch_models``) but never removed. The ``Maker.models`` relationship
# pointed at it, which caused ``session.delete(maker)`` to fail (SQLAlchemy's
# cascade loader queried a nonexistent ``models`` table). Class removed and
# the relationship renamed to ``Maker.watch_models``, pointing at the real
# class. No migration needed — the ``models`` table never existed in the DB.
