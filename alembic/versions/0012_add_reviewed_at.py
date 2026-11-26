"""add reviewed_at to makers

Adds `reviewed_at` (nullable timestamp) to the makers table.

Set when a maker is saved on /review (chunk 4N): a human has checked every
field on its card, so its values are authoritative. NULL means the maker is
still in the review queue. Both enrichment paths skip a reviewed maker —
is_independent has no NULL state for the never-overwrite rule to guard on, and
a field the reviewer deliberately cleared reads as missing, so the next
`cli enrich --commit` would refill it from the LLM cache.

No backfill: a maker already saved on /review can't be told apart reliably
from one whose values were never marked "auto" (enrichment before 0005 wrote
values without the marker), and wrongly marking a maker reviewed would hide it
from the queue. Every existing maker therefore starts unreviewed.

Revision ID: 0012
Revises: 0011
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.drop_column("reviewed_at")
