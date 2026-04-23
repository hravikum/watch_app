"""add scan_runs table

Records every invocation of run_scan(). One row per scan with status,
counters, and timing. Used by the `history` CLI and by scheduled scans
for monitoring.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "started_at", sa.DateTime(), nullable=False, index=True,
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False,
            server_default="running", index=True,
        ),
        sa.Column("sources_run", sa.Integer(), nullable=True),
        sa.Column("mentions_written", sa.Integer(), nullable=True),
        sa.Column("makers_scored", sa.Integer(), nullable=True),
        sa.Column(
            "trigger", sa.String(length=20), nullable=False,
            server_default="manual",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("scan_runs")
