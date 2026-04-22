"""fix mention unique constraint to include maker_id

The original constraint (source_id, url) was too strict — it prevented
recording multiple makers mentioned in the same article. The correct
constraint is (source_id, url, maker_id), which matches the orchestrator's
own dedup query and permits one Mention row per (maker, source, article).

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite can't drop a named unique constraint directly through op.drop_constraint;
    # use batch mode, which recreates the table with the new constraint set.
    with op.batch_alter_table("mentions") as batch_op:
        batch_op.drop_constraint("uq_mention_source_url", type_="unique")
        batch_op.create_unique_constraint(
            "uq_mention_source_url_maker",
            ["source_id", "url", "maker_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("mentions") as batch_op:
        batch_op.drop_constraint("uq_mention_source_url_maker", type_="unique")
        batch_op.create_unique_constraint(
            "uq_mention_source_url",
            ["source_id", "url"],
        )
