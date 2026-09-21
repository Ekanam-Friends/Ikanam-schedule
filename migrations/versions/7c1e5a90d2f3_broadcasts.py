"""broadcasts

Рассылки владельца и ответы на них. Ответы удаляются после выгрузки в CSV.

Revision ID: 7c1e5a90d2f3
Revises: 3f2a9c7d1b44
Create Date: 2026-09-21 12:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1e5a90d2f3"
down_revision: Union[str, Sequence[str], None] = "3f2a9c7d1b44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "broadcast_answers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("broadcast_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["broadcast_id"], ["broadcasts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("broadcast_answers", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_broadcast_answers_broadcast_id"), ["broadcast_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("broadcast_answers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_broadcast_answers_broadcast_id"))
    op.drop_table("broadcast_answers")
    op.drop_table("broadcasts")
