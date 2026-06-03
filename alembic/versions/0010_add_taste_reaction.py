"""add personal taste reaction to makers

Adds `taste_reaction` (nullable string) and `taste_reaction_at` (nullable
timestamp) to the makers table.

These are ORTHOGONAL to both suppression (0008) and relationship_state (0009).
relationship_state captures engagement posture; taste_reaction is a like/dislike
*taste* signal — the input the adaptive re-ranking (Layer 3b) will read — and
does NOT affect visibility. Both columns are nullable: an untouched maker has no
reaction (NULL) and no timestamp. `taste_reaction_at` records when the reaction
was set so a future digest / Layer 3b can window by it. Single-user/local; NULL
is the "unset" sentinel, so no server default is needed.

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.add_column(
            sa.Column("taste_reaction", sa.String(length=10), nullable=True)
        )
        batch_op.add_column(
            sa.Column("taste_reaction_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.drop_column("taste_reaction_at")
        batch_op.drop_column("taste_reaction")
