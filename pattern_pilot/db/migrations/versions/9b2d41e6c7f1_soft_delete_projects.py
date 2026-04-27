"""Soft-delete projects instead of hard-deleting review history.

Revision ID: 9b2d41e6c7f1
Revises: a7c9d0e4f1b2
Create Date: 2026-04-16
"""

from alembic import op
import sqlalchemy as sa


revision = "9b2d41e6c7f1"
down_revision = "a7c9d0e4f1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(), nullable=True))
    op.drop_index("ix_projects_name", table_name="projects")
    op.create_index(
        "ix_projects_name_active",
        "projects",
        ["name"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_projects_name_active", table_name="projects")
    op.create_index("ix_projects_name", "projects", ["name"], unique=True)
    op.drop_column("projects", "archived_at")
