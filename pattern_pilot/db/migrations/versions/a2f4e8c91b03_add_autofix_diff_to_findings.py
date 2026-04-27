"""add_autofix_diff_to_findings

Revision ID: a2f4e8c91b03
Revises: 163f7bcf6796
Create Date: 2026-03-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2f4e8c91b03"
down_revision: Union[str, None] = "163f7bcf6796"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("autofix_diff", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("findings", "autofix_diff")
