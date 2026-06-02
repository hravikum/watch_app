"""add personal relationship state + notes to makers

Adds `relationship_state` (nullable string) and `notes` (nullable text) to the
makers table.

These are ORTHOGONAL to suppression (0008). Suppression answers "should this
maker be in the tool at all"; relationship_state answers "what's my engagement
posture toward this real maker" — target / watching / known / pass — and does
NOT affect visibility. Both are nullable: an untouched maker has no state and
no notes. Single-user/local; no auth, no server default needed (NULL is the
"unset" sentinel).

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.add_column(
            sa.Column("relationship_state", sa.String(length=20), nullable=True)
        )
        batch_op.add_column(sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.drop_column("notes")
        batch_op.drop_column("relationship_state")
