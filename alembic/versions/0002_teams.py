"""teams and api key assignment

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("monthly_token_budget", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(sa.Column("team_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_api_keys_team_id",
            "teams",
            ["team_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_constraint("fk_api_keys_team_id", type_="foreignkey")
        batch.drop_column("team_id")
    op.drop_table("teams")
