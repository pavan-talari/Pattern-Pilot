"""Add review-run delete actions to dependent FKs.

Review rounds, findings, and submissions are run-scoped records and should be
deleted with their run. Event log rows keep their audit payload but lose the
run FK. Advisories are project-level records, so deleting a finding clears the
link instead of deleting the advisory.

Revision ID: e8b91c4a6d2f
Revises: c4a7d91b2e8f
Create Date: 2026-04-15
"""

from alembic import op


revision = "e8b91c4a6d2f"
down_revision = "c4a7d91b2e8f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("review_submissions_run_id_fkey", "review_submissions", type_="foreignkey")
    op.create_foreign_key(
        "review_submissions_run_id_fkey",
        "review_submissions",
        "review_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("review_rounds_run_id_fkey", "review_rounds", type_="foreignkey")
    op.create_foreign_key(
        "review_rounds_run_id_fkey",
        "review_rounds",
        "review_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("findings_round_id_fkey", "findings", type_="foreignkey")
    op.create_foreign_key(
        "findings_round_id_fkey",
        "findings",
        "review_rounds",
        ["round_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("findings_run_id_fkey", "findings", type_="foreignkey")
    op.create_foreign_key(
        "findings_run_id_fkey",
        "findings",
        "review_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("advisories_finding_id_fkey", "advisories", type_="foreignkey")
    op.create_foreign_key(
        "advisories_finding_id_fkey",
        "advisories",
        "findings",
        ["finding_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint("event_log_run_id_fkey", "event_log", type_="foreignkey")
    op.create_foreign_key(
        "event_log_run_id_fkey",
        "event_log",
        "review_runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("event_log_run_id_fkey", "event_log", type_="foreignkey")
    op.create_foreign_key(
        "event_log_run_id_fkey",
        "event_log",
        "review_runs",
        ["run_id"],
        ["id"],
    )

    op.drop_constraint("advisories_finding_id_fkey", "advisories", type_="foreignkey")
    op.create_foreign_key(
        "advisories_finding_id_fkey",
        "advisories",
        "findings",
        ["finding_id"],
        ["id"],
    )

    op.drop_constraint("findings_run_id_fkey", "findings", type_="foreignkey")
    op.create_foreign_key(
        "findings_run_id_fkey",
        "findings",
        "review_runs",
        ["run_id"],
        ["id"],
    )

    op.drop_constraint("findings_round_id_fkey", "findings", type_="foreignkey")
    op.create_foreign_key(
        "findings_round_id_fkey",
        "findings",
        "review_rounds",
        ["round_id"],
        ["id"],
    )

    op.drop_constraint("review_rounds_run_id_fkey", "review_rounds", type_="foreignkey")
    op.create_foreign_key(
        "review_rounds_run_id_fkey",
        "review_rounds",
        "review_runs",
        ["run_id"],
        ["id"],
    )

    op.drop_constraint("review_submissions_run_id_fkey", "review_submissions", type_="foreignkey")
    op.create_foreign_key(
        "review_submissions_run_id_fkey",
        "review_submissions",
        "review_runs",
        ["run_id"],
        ["id"],
    )
