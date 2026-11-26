"""add blocked_makers tombstone table

Creates `blocked_makers`: one row per hard-deleted maker, carrying its
canonical name and alias list as they stood at deletion, plus a timestamp
and optional reason.

Why: hard delete removes the Maker row and everything referencing it, but
left nothing behind that said "don't re-create this" — so the next scan's
resolver (step 3: create-if-no-match) resurrected the maker as soon as any
live article mentioned the name again. The tombstone closes that loop:
delete_maker writes a row here on commit, and the resolver checks it before
creating a NEW maker, using the same normalize + fuzzy >=90 semantics it
uses for matching — so variant spellings that would have resolved into the
deleted maker are blocked too. Matching of EXISTING makers is untouched,
and the seed loader deliberately bypasses the check (adding a blocked name
to known_makers.yaml is an explicit override). Reversible via `cli unblock`.

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blocked_makers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("canonical_name", sa.String(length=200), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("blocked_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_blocked_makers_canonical_name",
        "blocked_makers",
        ["canonical_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_blocked_makers_canonical_name", table_name="blocked_makers")
    op.drop_table("blocked_makers")
