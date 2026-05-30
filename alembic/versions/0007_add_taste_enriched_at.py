"""add taste_enriched_at column to makers

Adds a single nullable timestamp, `taste_enriched_at`, to the makers table.
It is the once-per-maker gate for the automatic taste-tagging of
auto-discovered makers (chunk 3F): the tagger stamps it the first time it
attempts a maker, whether or not the LLM produced any in-vocabulary tags, so
abstentions are never re-queried on later scans.

Kept separate from `enriched_at` (0005), which tracks the factual on-scan
enrichment (website / country / is_independent). Existing rows stay None.

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.add_column(
            sa.Column("taste_enriched_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.drop_column("taste_enriched_at")
