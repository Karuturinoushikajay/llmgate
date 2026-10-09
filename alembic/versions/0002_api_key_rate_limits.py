"""add per-key rate limits

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09
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
    op.add_column("api_keys", sa.Column("requests_per_minute", sa.Integer(), nullable=True))
    op.add_column("api_keys", sa.Column("tokens_per_minute", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "tokens_per_minute")
    op.drop_column("api_keys", "requests_per_minute")
