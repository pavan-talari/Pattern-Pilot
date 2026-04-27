"""SQLAlchemy 2.0 models — all 7 Pattern Pilot tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Shared base for all ORM models."""

    pass


# ── 1. Projects ──────────────────────────────────────────────────────────────


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False, default="filesystem")
    connector_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    governance_paths: Mapped[list[str]] = mapped_column(JSONB, default=list)
    completion_gates: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    tech_stack: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reviewer_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reviewer_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewer_reasoning_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    review_runs: Mapped[list[ReviewRun]] = relationship(back_populates="project")
    advisories: Mapped[list[Advisory]] = relationship(back_populates="project")
    events: Mapped[list[EventLog]] = relationship(back_populates="project")


# ── 2. Review Submissions ────────────────────────────────────────────────────


class ReviewSubmission(Base):
    __tablename__ = "review_submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=True
    )
    submission_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    diff_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    files_changed: Mapped[list[str]] = mapped_column(JSONB, default=list)
    deterministic_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    deterministic_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    progressed_to_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    run: Mapped[ReviewRun | None] = relationship(back_populates="submissions")


# ── 3. Review Runs ───────────────────────────────────────────────────────────


class ReviewRun(Base):
    __tablename__ = "review_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False
    )
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending"
    )
    verdict: Mapped[str | None] = mapped_column(String(50), nullable=True)
    review_profile: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")
    governance_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False, default="v1.0")
    diff_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_title_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_status_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    project_context_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False, default="filesystem")
    connector_capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list)
    total_submissions: Mapped[int] = mapped_column(Integer, default=0)
    total_rounds: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    project: Mapped[Project] = relationship(back_populates="review_runs")
    submissions: Mapped[list[ReviewSubmission]] = relationship(back_populates="run")
    rounds: Mapped[list[ReviewRound]] = relationship(back_populates="run")
    findings: Mapped[list[Finding]] = relationship(back_populates="run")
    events: Mapped[list[EventLog]] = relationship(
        back_populates="run", foreign_keys="EventLog.run_id"
    )


# ── 4. Review Rounds ─────────────────────────────────────────────────────────


class ReviewRound(Base):
    __tablename__ = "review_rounds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    verdict: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_used: Mapped[str] = mapped_column(String(50), nullable=False, default="gpt-4o")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    run: Mapped[ReviewRun] = relationship(back_populates="rounds")
    findings: Mapped[list[Finding]] = relationship(back_populates="round")


# ── 5. Findings ──────────────────────────────────────────────────────────────


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    round_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_rounds.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    tier: Mapped[str] = mapped_column(String(30), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    autofix_safe: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    rule_refs: Mapped[list[str]] = mapped_column(JSONB, default=list)
    why_now: Mapped[str | None] = mapped_column(Text, nullable=True)
    autofix_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="open")
    human_override: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    round: Mapped[ReviewRound] = relationship(back_populates="findings")
    run: Mapped[ReviewRun] = relationship(back_populates="findings")


# ── 6. Advisories ────────────────────────────────────────────────────────────


class Advisory(Base):
    __tablename__ = "advisories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False
    )
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    finding_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("findings.id", ondelete="SET NULL"), nullable=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    project: Mapped[Project] = relationship(back_populates="advisories")


# ── 7. Event Log ─────────────────────────────────────────────────────────────


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("review_runs.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    project: Mapped[Project | None] = relationship(back_populates="events")
    run: Mapped[ReviewRun | None] = relationship(back_populates="events")
