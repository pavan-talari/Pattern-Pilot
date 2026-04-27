"""Add per-project reviewer model configuration.

Revision ID: a7c9d0e4f1b2
Revises: f6d3a29b8c10
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa


revision = "a7c9d0e4f1b2"
down_revision = "f6d3a29b8c10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("reviewer_provider", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("reviewer_model", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("reviewer_reasoning_effort", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "reviewer_reasoning_effort")
    op.drop_column("projects", "reviewer_model")
    op.drop_column("projects", "reviewer_provider")
