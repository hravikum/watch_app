"""add enrichment tracking columns to makers

Adds `enriched_at` and `enriched_fields` to the makers table to track which
fields were populated by LLM-driven enrichment vs by the seed YAML or manual
SQL. Both nullable — existing rows stay None.

See chunk 2S for context. Used by chunk 2T's automatic enrichment workflow.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.add_column(
            sa.Column("enriched_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("enriched_fields", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.drop_column("enriched_fields")
        batch_op.drop_column("enriched_at")
