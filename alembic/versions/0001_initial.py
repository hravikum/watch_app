"""initial schema

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "makers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("canonical_name", sa.String(200), nullable=False),
        sa.Column("aliases", sa.JSON, nullable=True),
        sa.Column("country", sa.String(100), nullable=True),
        sa.Column("founded_year", sa.Integer, nullable=True),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("instagram_handle", sa.String(100), nullable=True),
        sa.Column("youtube_channel", sa.String(200), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("is_independent", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("production_volume_estimate", sa.Integer, nullable=True),
        sa.Column("first_seen_at", sa.DateTime, nullable=False),
        sa.Column("last_scored_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_makers_canonical_name", "makers", ["canonical_name"], unique=True)

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("prestige_weight", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_scraped_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_sources_name", "sources", ["name"], unique=True)

    op.create_table(
        "mentions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("maker_id", sa.Integer, sa.ForeignKey("makers.id"), nullable=False),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("excerpt", sa.Text, nullable=True),
        sa.Column("published_at", sa.DateTime, nullable=True),
        sa.Column("discovered_at", sa.DateTime, nullable=False),
        sa.Column("raw_text", sa.Text, nullable=True),
        sa.Column("extraction_confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.UniqueConstraint("source_id", "url", name="uq_mention_source_url"),
    )
    op.create_index("ix_mentions_maker_id", "mentions", ["maker_id"])
    op.create_index("ix_mentions_source_id", "mentions", ["source_id"])
    op.create_index("ix_mentions_published_at", "mentions", ["published_at"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("maker_id", sa.Integer, sa.ForeignKey("makers.id"), nullable=False),
        sa.Column("signal_type", sa.String(100), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("computed_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_signals_maker_id", "signals", ["maker_id"])
    op.create_index("ix_signals_signal_type", "signals", ["signal_type"])
    op.create_index("ix_signals_computed_at", "signals", ["computed_at"])

    op.create_table(
        "scores",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("maker_id", sa.Integer, sa.ForeignKey("makers.id"), nullable=False),
        sa.Column("preset", sa.String(50), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("rank", sa.Integer, nullable=True),
        sa.Column("computed_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_scores_maker_id", "scores", ["maker_id"])
    op.create_index("ix_scores_preset", "scores", ["preset"])
    op.create_index("ix_scores_computed_at", "scores", ["computed_at"])

    op.create_table(
        "llm_cache",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("response", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("llm_cache")
    op.drop_index("ix_scores_computed_at", table_name="scores")
    op.drop_index("ix_scores_preset", table_name="scores")
    op.drop_index("ix_scores_maker_id", table_name="scores")
    op.drop_table("scores")
    op.drop_index("ix_signals_computed_at", table_name="signals")
    op.drop_index("ix_signals_signal_type", table_name="signals")
    op.drop_index("ix_signals_maker_id", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_mentions_published_at", table_name="mentions")
    op.drop_index("ix_mentions_source_id", table_name="mentions")
    op.drop_index("ix_mentions_maker_id", table_name="mentions")
    op.drop_table("mentions")
    op.drop_index("ix_sources_name", table_name="sources")
    op.drop_table("sources")
    op.drop_index("ix_makers_canonical_name", table_name="makers")
    op.drop_table("makers")
