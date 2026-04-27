"""Add partial unique index for linked advisories.

Each finding can produce at most one advisory. NULL finding_id values are
allowed for preserved advisories whose originating finding was deleted.

Revision ID: f6d3a29b8c10
Revises: e8b91c4a6d2f
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa


revision = "f6d3a29b8c10"
down_revision = "e8b91c4a6d2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Prerequisite for existing environments: verify this returns no rows before upgrade:
    # SELECT finding_id, COUNT(*) FROM advisories WHERE finding_id IS NOT NULL
    # GROUP BY finding_id HAVING COUNT(*) > 1;
    op.create_index(
        "advisories_finding_id_unique",
        "advisories",
        ["finding_id"],
        unique=True,
        postgresql_where=sa.text("finding_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("advisories_finding_id_unique", table_name="advisories")
