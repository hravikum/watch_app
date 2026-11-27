"""add web-research audit columns to makers

Adds six nullable columns to the makers table for the web-researched audit
(chunk 4O):

- `audit_verdict` (string): "yes" / "no" / "unsure" — is this a real, active
  wristwatch maker?
- `audit_reason` (string): one line explaining the verdict.
- `audit_brand` (string): the brand a person, partial or model name actually
  belongs to, when it differs from the maker's name — so the review card can
  point at a rename or a merge.
- `audit_sources` (JSON): the pages the research relied on, [{url, title}],
  limited to URLs the web search actually returned.
- `audited_at` (timestamp): the once-per-maker gate for the audit pass.
- `is_pre_launch` (boolean): announced but not delivering yet. Drafted by the
  audit, editable on /review.

The verdict is informational — nothing is suppressed automatically. All
columns are nullable with no backfill: NULL means "not audited yet".

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.add_column(sa.Column("audit_verdict", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("audit_reason", sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column("audit_brand", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("audit_sources", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("audited_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("is_pre_launch", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("makers") as batch_op:
        batch_op.drop_column("is_pre_launch")
        batch_op.drop_column("audited_at")
        batch_op.drop_column("audit_sources")
        batch_op.drop_column("audit_brand")
        batch_op.drop_column("audit_reason")
        batch_op.drop_column("audit_verdict")
