"""add watch_models table

Stores parsed watch model data from GPHG nominee detail pages. One row per
distinct GPHG watch URL. Specs are pulled from the structured label/value
content on each detail page; missing fields stay NULL.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watch_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "maker_id", sa.Integer(),
            sa.ForeignKey("makers.id"), nullable=False, index=True,
        ),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("gphg_year", sa.Integer(), nullable=False, index=True),
        sa.Column("gphg_url", sa.String(length=500), nullable=False),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("case_material", sa.String(length=200), nullable=True),
        sa.Column("case_diameter_mm", sa.Float(), nullable=True),
        sa.Column("movement_caliber", sa.String(length=200), nullable=True),
        sa.Column("movement_type", sa.String(length=100), nullable=True),
        sa.Column("frequency_hz", sa.Float(), nullable=True),
        sa.Column("power_reserve_hours", sa.Integer(), nullable=True),
        sa.Column("water_resistance_m", sa.Integer(), nullable=True),
        sa.Column("complications", sa.JSON(), nullable=True),
        sa.Column("price_chf", sa.Integer(), nullable=True),
        sa.Column("limited_edition_count", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("gphg_url", name="uq_watch_models_gphg_url"),
    )


def downgrade() -> None:
    op.drop_table("watch_models")
