"""Add task_id, decision_id, attempt_number to review_runs.

Phase 1 of context-based workflow: stable task identity for run continuity.

Revision ID: b3c1d7e20f45
Revises: a2f4e8c91b03
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "b3c1d7e20f45"
down_revision = "a2f4e8c91b03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("task_id", sa.String(255), nullable=True))
    op.add_column("review_runs", sa.Column("decision_id", sa.String(255), nullable=True))
    op.add_column("review_runs", sa.Column("attempt_number", sa.Integer(), nullable=True))
    op.create_index("ix_review_runs_task_id", "review_runs", ["task_id"])
    op.create_index("ix_review_runs_decision_id", "review_runs", ["decision_id"])


def downgrade() -> None:
    op.drop_index("ix_review_runs_decision_id", "review_runs")
    op.drop_index("ix_review_runs_task_id", "review_runs")
    op.drop_column("review_runs", "attempt_number")
    op.drop_column("review_runs", "decision_id")
    op.drop_column("review_runs", "task_id")
