"""Review history routes — runs, rounds, findings, and submit."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pattern_pilot.core.contracts import SubmitRequest, SubmitResponse
from pattern_pilot.core.orchestrator import Orchestrator
from pattern_pilot.db import models
from pattern_pilot.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────


class FindingResponse(BaseModel):
    id: str
    tier: str
    category: str
    file_path: str
    line_start: int | None
    line_end: int | None
    message: str
    suggestion: str | None
    autofix_safe: bool
    severity: str
    confidence: float
    rule_refs: list[str]
    why_now: str | None
    status: str
    human_override: str | None

    model_config = {"from_attributes": True}


class RoundResponse(BaseModel):
    id: str
    round_number: int
    verdict: str | None
    model_used: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    duration_ms: int
    findings_count: int = 0

    model_config = {"from_attributes": True}


class RunResponse(BaseModel):
    id: str
    project_id: str
    task_id: str | None = None
    task_ref: str
    status: str
    verdict: str | None
    failure_kind: str | None = None
    failure_reason: str | None = None
    review_profile: str
    total_rounds: int
    total_submissions: int
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class RunDetailResponse(RunResponse):
    governance_snapshot: dict[str, Any]
    prompt_version: str
    diff_hash: str | None
    connector_type: str


class RunHistoryResponse(BaseModel):
    id: str
    project_name: str
    project_id: str
    task_id: str | None
    task_ref: str
    status: str
    verdict: str | None
    failure_kind: str | None = None
    failure_reason: str | None = None
    total_rounds: int
    total_submissions: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime | None


class SubmissionResponse(BaseModel):
    id: str
    submission_number: int
    diff_hash: str
    files_changed: list[str]
    deterministic_results: list[dict[str, Any]]
    deterministic_passed: bool
    progressed_to_llm: bool
    created_at: datetime | None

    model_config = {"from_attributes": True}


def _failure_reason(event: models.EventLog | None) -> str | None:
    if not event or not event.payload:
        return None
    payload = event.payload
    return payload.get("error") or payload.get("reason")


def _run_failure_metadata(
    run: models.ReviewRun,
    latest_submission: models.ReviewSubmission | None,
    latest_event: models.EventLog | None,
) -> tuple[str | None, str | None]:
    if (
        run.status == "failed"
        and latest_submission
        and latest_submission.deterministic_passed is False
        and latest_submission.progressed_to_llm is False
    ):
        return "deterministic_checks", _failure_reason(latest_event)
    if run.status == "reviewer_error":
        return "reviewer_infrastructure", _failure_reason(latest_event)
    if (
        run.status == "failed"
        and latest_event
        and latest_event.payload
        and latest_event.payload.get("phase") == "reviewer"
    ):
        return "reviewer_infrastructure", _failure_reason(latest_event)
    if run.status == "failed":
        return None, _failure_reason(latest_event)
    return None, None


def _attach_failure_kinds(
    runs: list[models.ReviewRun],
    latest_submissions: dict[str, models.ReviewSubmission],
    latest_events: dict[str, models.EventLog],
) -> list[RunResponse]:
    response: list[RunResponse] = []
    for run in runs:
        payload = RunResponse.model_validate(run)
        latest_submission = latest_submissions.get(run.id)
        latest_event = latest_events.get(run.id)
        payload.failure_kind, payload.failure_reason = _run_failure_metadata(
            run, latest_submission, latest_event
        )
        response.append(payload)
    return response


def _run_failure_kind(
    run: models.ReviewRun,
    latest_submission: models.ReviewSubmission | None,
    latest_event: models.EventLog | None,
) -> tuple[str | None, str | None]:
    return _run_failure_metadata(run, latest_submission, latest_event)


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/reviews/submit", response_model=SubmitResponse)
async def submit_review(
    request: SubmitRequest, session: AsyncSession = Depends(get_session)
) -> SubmitResponse:
    """Submit code for review — triggers the full QC loop."""
    try:
        orchestrator = Orchestrator(session)
        result = await orchestrator.submit_for_review(request)
        await session.commit()
        return result
    except Exception as exc:
        logger.exception("Review submission failed")
        await session.rollback()
        raise HTTPException(500, detail=f"Review failed: {exc}") from exc


@router.get("/projects/{project_id}/reviews", response_model=list[RunResponse])
async def list_reviews(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """List review runs for a project."""
    result = await session.execute(
        select(models.ReviewRun)
        .where(models.ReviewRun.project_id == project_id)
        .order_by(models.ReviewRun.created_at.desc())
    )
    runs = result.scalars().all()
    run_ids = [run.id for run in runs]

    latest_submissions: dict[str, models.ReviewSubmission] = {}
    if run_ids:
        submissions_result = await session.execute(
            select(models.ReviewSubmission)
            .where(models.ReviewSubmission.run_id.in_(run_ids))
            .order_by(
                models.ReviewSubmission.run_id,
                models.ReviewSubmission.submission_number.desc(),
            )
        )
        for submission in submissions_result.scalars().all():
            if submission.run_id and submission.run_id not in latest_submissions:
                latest_submissions[submission.run_id] = submission

    latest_events: dict[str, models.EventLog] = {}
    if run_ids:
        events_result = await session.execute(
            select(models.EventLog)
            .where(
                models.EventLog.run_id.in_(run_ids),
                models.EventLog.event_type.in_(
                    ["run_failed", "run_auto_expired", "run_reviewer_error"]
                ),
            )
            .order_by(models.EventLog.run_id, models.EventLog.created_at.desc())
        )
        for event in events_result.scalars().all():
            if event.run_id and event.run_id not in latest_events:
                latest_events[event.run_id] = event

    return _attach_failure_kinds(runs, latest_submissions, latest_events)


@router.get("/reviews/runs", response_model=list[RunHistoryResponse])
async def list_runs(
    task_id: str,
    project_name: str | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[RunHistoryResponse]:
    """List historical runs for a stable task identity.

    Falls back to legacy `task_ref == task_id` rows when `task_id` was not stored.
    """
    query = (
        select(models.ReviewRun, models.Project.name)
        .join(models.Project, models.Project.id == models.ReviewRun.project_id)
        .where(
            or_(
                models.ReviewRun.task_id == task_id,
                and_(
                    models.ReviewRun.task_id.is_(None),
                    models.ReviewRun.task_ref == task_id,
                ),
            )
        )
        .order_by(models.ReviewRun.created_at.desc())
        .limit(limit)
    )
    if project_name:
        query = query.where(models.Project.name == project_name)

    rows = (await session.execute(query)).all()
    if not rows:
        return []

    runs = [run for run, _ in rows]
    project_names = {run.id: name for run, name in rows}
    run_ids = [run.id for run in runs]

    latest_submissions: dict[str, models.ReviewSubmission] = {}
    submissions_result = await session.execute(
        select(models.ReviewSubmission)
        .where(models.ReviewSubmission.run_id.in_(run_ids))
        .order_by(
            models.ReviewSubmission.run_id,
            models.ReviewSubmission.submission_number.desc(),
        )
    )
    for submission in submissions_result.scalars().all():
        if submission.run_id and submission.run_id not in latest_submissions:
            latest_submissions[submission.run_id] = submission

    latest_events: dict[str, models.EventLog] = {}
    events_result = await session.execute(
        select(models.EventLog)
        .where(
            models.EventLog.run_id.in_(run_ids),
            models.EventLog.event_type.in_(
                ["run_failed", "run_auto_expired", "run_reviewer_error"]
            ),
        )
        .order_by(models.EventLog.run_id, models.EventLog.created_at.desc())
    )
    for event in events_result.scalars().all():
        if event.run_id and event.run_id not in latest_events:
            latest_events[event.run_id] = event

    response: list[RunHistoryResponse] = []
    for run in runs:
        latest_submission = latest_submissions.get(run.id)
        latest_event = latest_events.get(run.id)
        failure_kind, failure_reason = _run_failure_kind(
            run, latest_submission, latest_event
        )
        response.append(
            RunHistoryResponse(
                id=run.id,
                project_name=project_names.get(run.id, ""),
                project_id=run.project_id,
                task_id=run.task_id,
                task_ref=run.task_ref,
                status=run.status,
                verdict=run.verdict,
                failure_kind=failure_kind,
                failure_reason=failure_reason,
                total_rounds=run.total_rounds,
                total_submissions=run.total_submissions,
                started_at=run.started_at,
                completed_at=run.completed_at,
                created_at=run.created_at,
            )
        )
    return response


@router.get("/reviews/{run_id}", response_model=RunDetailResponse)
async def get_review(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """Get full detail of a review run."""
    run = await session.get(models.ReviewRun, run_id)
    if not run:
        raise HTTPException(404, "Review run not found")
    submissions_result = await session.execute(
        select(models.ReviewSubmission)
        .where(models.ReviewSubmission.run_id == run_id)
        .order_by(models.ReviewSubmission.submission_number.desc())
        .limit(1)
    )
    latest_submission = submissions_result.scalar_one_or_none()
    events_result = await session.execute(
        select(models.EventLog)
        .where(
            models.EventLog.run_id == run_id,
            models.EventLog.event_type.in_(
                ["run_failed", "run_auto_expired", "run_reviewer_error"]
            ),
        )
        .order_by(models.EventLog.created_at.desc())
        .limit(1)
    )
    latest_event = events_result.scalar_one_or_none()
    payload = RunDetailResponse.model_validate(run)
    payload.failure_kind, payload.failure_reason = _run_failure_kind(
        run, latest_submission, latest_event
    )
    return payload


@router.get("/reviews/{run_id}/rounds", response_model=list[RoundResponse])
async def get_rounds(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> list[RoundResponse]:
    """Get all rounds for a review run."""
    result = await session.execute(
        select(models.ReviewRound)
        .where(models.ReviewRound.run_id == run_id)
        .order_by(models.ReviewRound.round_number)
    )
    rounds = result.scalars().all()
    round_ids = [rnd.id for rnd in rounds]

    findings_by_round: dict[str, int] = {}
    if round_ids:
        counts_result = await session.execute(
            select(models.Finding.round_id, func.count(models.Finding.id))
            .where(models.Finding.round_id.in_(round_ids))
            .group_by(models.Finding.round_id)
        )
        findings_by_round = {
            round_id: count for round_id, count in counts_result.all()
        }

    response = []
    for rnd in rounds:
        resp = RoundResponse.model_validate(rnd)
        resp.findings_count = findings_by_round.get(rnd.id, 0)
        response.append(resp)

    return response


@router.get("/reviews/{run_id}/findings", response_model=list[FindingResponse])
async def get_findings(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """Get all findings for a review run."""
    result = await session.execute(
        select(models.Finding)
        .where(models.Finding.run_id == run_id)
        .order_by(models.Finding.created_at)
    )
    return result.scalars().all()


@router.get("/reviews/{run_id}/submissions", response_model=list[SubmissionResponse])
async def get_submissions(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """Get all submission attempts for a review run, including deterministic results."""
    result = await session.execute(
        select(models.ReviewSubmission)
        .where(models.ReviewSubmission.run_id == run_id)
        .order_by(models.ReviewSubmission.submission_number)
    )
    return result.scalars().all()
