"""Metrics routes — pass rates, costs, trends."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pattern_pilot.db import models
from pattern_pilot.db.session import get_session

router = APIRouter()


SUCCESS_STATUSES = {"passed", "passed_with_advisories"}
QC_COMPLETED_STATUSES = {"passed", "passed_with_advisories", "escalated"}


def _cost_metric(total_cost: float | None, cost_count: int) -> tuple[float | None, bool]:
    """Return displayed cost and whether any configured-cost data exists."""
    if cost_count <= 0 or total_cost is None:
        return None, False
    return round(total_cost, 4), True


@dataclass(frozen=True)
class RunMetricValues:
    total_runs: int
    completed_runs: int
    successful_runs: int
    abandoned_runs: int
    pass_rate: float
    avg_rounds: float
    first_round_success_rate: float
    round_distribution: list[RoundDistribution]


class RoundDistribution(BaseModel):
    total_rounds: int
    runs: int
    successful: int


def _run_metrics(runs: Sequence[Any]) -> RunMetricValues:
    total_runs = len(runs)
    completed = [r for r in runs if r.status in QC_COMPLETED_STATUSES]
    successful = [r for r in completed if r.status in SUCCESS_STATUSES]
    abandoned_runs = sum(1 for r in runs if r.status == "abandoned")

    completed_runs = len(completed)
    pass_rate = len(successful) / completed_runs if completed_runs else 0.0
    avg_rounds = (
        sum((r.total_rounds or 0) for r in completed) / completed_runs
        if completed_runs
        else 0.0
    )
    first_round_successes = sum(1 for r in successful if (r.total_rounds or 0) == 1)
    first_round_success_rate = (
        first_round_successes / len(successful)
        if successful
        else 0.0
    )

    round_numbers = sorted({r.total_rounds or 0 for r in completed})
    round_distribution = [
        RoundDistribution(
            total_rounds=rounds,
            runs=sum(1 for r in completed if (r.total_rounds or 0) == rounds),
            successful=sum(
                1
                for r in successful
                if (r.total_rounds or 0) == rounds
            ),
        )
        for rounds in round_numbers
    ]

    return RunMetricValues(
        total_runs=total_runs,
        completed_runs=completed_runs,
        successful_runs=len(successful),
        abandoned_runs=abandoned_runs,
        pass_rate=round(pass_rate, 3),
        avg_rounds=round(avg_rounds, 2),
        first_round_success_rate=round(first_round_success_rate, 3),
        round_distribution=round_distribution,
    )


class ProjectMetrics(BaseModel):
    project_id: str
    project_name: str
    total_runs: int
    completed_runs: int
    abandoned_runs: int
    deterministic_failures: int
    passed: int
    passed_with_advisories: int
    escalated: int
    failed: int
    pass_rate: float
    avg_rounds: float
    first_round_success_rate: float
    round_distribution: list[RoundDistribution]
    total_cost_usd: float | None
    cost_configured: bool


class SummaryMetrics(BaseModel):
    total_projects: int
    total_runs: int
    completed_runs: int
    abandoned_runs: int
    deterministic_failures: int
    overall_pass_rate: float
    avg_rounds: float
    first_round_success_rate: float
    round_distribution: list[RoundDistribution]
    total_cost_usd: float | None
    cost_configured: bool


@router.get("/projects/{project_id}/metrics", response_model=ProjectMetrics)
async def get_project_metrics(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> ProjectMetrics:
    """Get pass rates, avg rounds, and cost for a project."""
    project = await session.get(models.Project, project_id)
    name = project.name if project else "unknown"

    runs_result = await session.execute(
        select(models.ReviewRun).where(models.ReviewRun.project_id == project_id)
    )
    runs = runs_result.scalars().all()
    metrics = _run_metrics(runs)

    passed = sum(1 for r in runs if r.status == "passed")
    passed_adv = sum(1 for r in runs if r.status == "passed_with_advisories")
    escalated = sum(1 for r in runs if r.status == "escalated")
    failed = sum(1 for r in runs if r.status == "failed")

    run_ids = [r.id for r in runs]
    if run_ids:
        deterministic_result = await session.execute(
            select(func.count(models.ReviewSubmission.id)).where(
                models.ReviewSubmission.run_id.in_(run_ids),
                models.ReviewSubmission.deterministic_passed.is_(False),
            )
        )
        deterministic_failures = deterministic_result.scalar() or 0
        cost_count_result = await session.execute(
            select(func.count(models.ReviewRound.id)).where(
                models.ReviewRound.run_id.in_(run_ids),
                models.ReviewRound.cost_usd.is_not(None),
            )
        )
        cost_count = cost_count_result.scalar() or 0
        rounds_result = await session.execute(
            select(func.sum(models.ReviewRound.cost_usd)).where(
                models.ReviewRound.run_id.in_(run_ids),
                models.ReviewRound.cost_usd.is_not(None),
            )
        )
        total_cost_raw = rounds_result.scalar()
    else:
        deterministic_failures = 0
        cost_count = 0
        total_cost_raw = None
    total_cost, cost_configured = _cost_metric(total_cost_raw, cost_count)

    return ProjectMetrics(
        project_id=project_id,
        project_name=name,
        total_runs=metrics.total_runs,
        completed_runs=metrics.completed_runs,
        abandoned_runs=metrics.abandoned_runs,
        deterministic_failures=deterministic_failures,
        passed=passed,
        passed_with_advisories=passed_adv,
        escalated=escalated,
        failed=failed,
        pass_rate=metrics.pass_rate,
        avg_rounds=metrics.avg_rounds,
        first_round_success_rate=metrics.first_round_success_rate,
        round_distribution=metrics.round_distribution,
        total_cost_usd=total_cost,
        cost_configured=cost_configured,
    )


@router.get("/metrics/summary", response_model=SummaryMetrics)
async def get_summary_metrics(
    session: AsyncSession = Depends(get_session),
) -> SummaryMetrics:
    """Cross-project summary metrics."""
    projects_count = await session.execute(
        select(func.count(models.Project.id)).where(models.Project.archived_at.is_(None))
    )
    total_projects = projects_count.scalar() or 0

    runs_result = await session.execute(select(models.ReviewRun))
    runs = runs_result.scalars().all()
    metrics = _run_metrics(runs)

    deterministic_result = await session.execute(
        select(func.count(models.ReviewSubmission.id)).where(
            models.ReviewSubmission.deterministic_passed.is_(False)
        )
    )
    deterministic_failures = deterministic_result.scalar() or 0

    cost_count_result = await session.execute(
        select(func.count(models.ReviewRound.id)).where(
            models.ReviewRound.cost_usd.is_not(None)
        )
    )
    cost_count = cost_count_result.scalar() or 0
    cost_result = await session.execute(
        select(func.sum(models.ReviewRound.cost_usd)).where(
            models.ReviewRound.cost_usd.is_not(None)
        )
    )
    total_cost_raw = cost_result.scalar()
    total_cost, cost_configured = _cost_metric(total_cost_raw, cost_count)

    return SummaryMetrics(
        total_projects=total_projects,
        total_runs=metrics.total_runs,
        completed_runs=metrics.completed_runs,
        abandoned_runs=metrics.abandoned_runs,
        deterministic_failures=deterministic_failures,
        overall_pass_rate=metrics.pass_rate,
        avg_rounds=metrics.avg_rounds,
        first_round_success_rate=metrics.first_round_success_rate,
        round_distribution=metrics.round_distribution,
        total_cost_usd=total_cost,
        cost_configured=cost_configured,
    )
