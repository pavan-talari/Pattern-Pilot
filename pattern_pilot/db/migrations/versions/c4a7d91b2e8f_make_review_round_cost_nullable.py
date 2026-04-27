"""Make review_rounds.cost_usd nullable.

Existing non-null cost estimates are preserved as historical estimates. Future rows
store NULL until explicit pricing config is provided.

Revision ID: c4a7d91b2e8f
Revises: b3c1d7e20f45
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa


revision = "c4a7d91b2e8f"
down_revision = "b3c1d7e20f45"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "review_rounds",
        "cost_usd",
        existing_type=sa.Float(),
        nullable=True,
        server_default=None,
    )


def downgrade() -> None:
    op.execute("UPDATE review_rounds SET cost_usd = 0 WHERE cost_usd IS NULL")
    op.alter_column(
        "review_rounds",
        "cost_usd",
        existing_type=sa.Float(),
        nullable=False,
        server_default=None,
    )
