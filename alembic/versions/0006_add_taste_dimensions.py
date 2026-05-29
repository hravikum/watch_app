"""add taste / character dimension columns to makers

Adds the seven curated taste dimensions to the makers table — two JSON
multi-tag columns (aesthetic_tags, complication_focus) and five single-value
string columns (maker_type, price_band, output_scale, distribution,
movement_origin). All nullable: existing rows stay None until tagged via the
seed YAML.

Foundation for the discovery -> engagement arc (Layer 1a). The controlled
vocabulary these columns accept lives in watch_app/taste.py.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.add_column(sa.Column("aesthetic_tags", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("complication_focus", sa.JSON(), nullable=True)
        )
        batch_op.add_column(sa.Column("maker_type", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("price_band", sa.String(length=20), nullable=True))
        batch_op.add_column(
            sa.Column("output_scale", sa.String(length=20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("distribution", sa.String(length=30), nullable=True)
        )
        batch_op.add_column(
            sa.Column("movement_origin", sa.String(length=20), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.drop_column("movement_origin")
        batch_op.drop_column("distribution")
        batch_op.drop_column("output_scale")
        batch_op.drop_column("price_band")
        batch_op.drop_column("maker_type")
        batch_op.drop_column("complication_focus")
        batch_op.drop_column("aesthetic_tags")
