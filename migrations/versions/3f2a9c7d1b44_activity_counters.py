"""activity counters

Счётчики событий по часам для сводки владельца. Без людей: день, час, вид, число.

Revision ID: 3f2a9c7d1b44
Revises: b376b1e97852
Create Date: 2026-09-07 09:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f2a9c7d1b44"
down_revision: Union[str, Sequence[str], None] = "b376b1e97852"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "activity_counters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day", "hour", "kind", name="uq_activity_slot"),
    )
    with op.batch_alter_table("activity_counters", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_activity_counters_day"), ["day"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("activity_counters", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_activity_counters_day"))
    op.drop_table("activity_counters")
