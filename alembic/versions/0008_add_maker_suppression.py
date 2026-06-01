"""add suppression columns to makers

Adds `is_suppressed` (bool, default false) and `suppressed_reason` (nullable
string) to the makers table.

A suppressed maker is hidden from the /discoveries view and from score-based
rankings (top / movers), but its row and mentions are KEPT. Because the row
survives, the resolver re-matches the name on a later scan instead of
re-creating the maker, so a one-off article mention can't resurrect it — and
the suppression is fully reversible with no data loss.

Mirrors the boolean-with-server-default pattern used for `is_independent` in
0001 so existing rows backfill to false.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_suppressed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("suppressed_reason", sa.String(length=300), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.drop_column("suppressed_reason")
        batch_op.drop_column("is_suppressed")
