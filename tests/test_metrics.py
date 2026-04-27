"""Tests for metrics cost display semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pattern_pilot.api.routes.metrics import (
    _cost_metric,
    _run_metrics,
    get_summary_metrics,
)


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ExecuteResult:
    def __init__(self, scalar_value=None, rows=None):
        self._scalar_value = scalar_value
        self._rows = rows or []

    def scalar(self):
        return self._scalar_value

    def scalars(self):
        return _ScalarRows(self._rows)


def test_cost_metric_unavailable_when_no_configured_costs():
    total_cost, configured = _cost_metric(None, 0)

    assert total_cost is None
    assert configured is False


def test_cost_metric_preserves_real_zero_when_configured():
    total_cost, configured = _cost_metric(0.0, 1)

    assert total_cost == 0.0
    assert configured is True


def test_cost_metric_rounds_configured_total():
    total_cost, configured = _cost_metric(1.23456, 2)

    assert total_cost == 1.2346
    assert configured is True


def test_run_metrics_pass_rate_excludes_pre_review_failures():
    runs = [
        SimpleNamespace(status="passed", total_rounds=1),
        SimpleNamespace(status="passed_with_advisories", total_rounds=2),
        SimpleNamespace(status="failed", total_rounds=0),
        SimpleNamespace(status="escalated", total_rounds=0),
        SimpleNamespace(status="abandoned", total_rounds=1),
    ]

    metrics = _run_metrics(runs)

    assert metrics.total_runs == 5
    assert metrics.completed_runs == 3
    assert metrics.abandoned_runs == 1
    assert metrics.pass_rate == 0.667


def test_run_metrics_round_distribution_counts_qc_reviewed_runs_only():
    runs = [
        SimpleNamespace(status="passed", total_rounds=1),
        SimpleNamespace(status="passed", total_rounds=2),
        SimpleNamespace(status="failed", total_rounds=0),
        SimpleNamespace(status="abandoned", total_rounds=1),
    ]

    metrics = _run_metrics(runs)

    assert [row.model_dump() for row in metrics.round_distribution] == [
        {"total_rounds": 1, "runs": 1, "successful": 1},
        {"total_rounds": 2, "runs": 1, "successful": 1},
    ]
    assert metrics.first_round_success_rate == 0.5


def test_run_metrics_excludes_failed_runs_even_after_rounds_started():
    runs = [
        SimpleNamespace(status="passed", total_rounds=1),
        SimpleNamespace(status="failed", total_rounds=2),
        SimpleNamespace(status="escalated", total_rounds=3),
    ]

    metrics = _run_metrics(runs)

    assert metrics.completed_runs == 2
    assert metrics.pass_rate == 0.5
    assert [row.model_dump() for row in metrics.round_distribution] == [
        {"total_rounds": 1, "runs": 1, "successful": 1},
        {"total_rounds": 3, "runs": 1, "successful": 0},
    ]


@pytest.mark.asyncio
async def test_summary_metrics_reports_deterministic_failures_separately():
    session = SimpleNamespace()
    results = iter([
        _ExecuteResult(scalar_value=1),
        _ExecuteResult(
            rows=[
                SimpleNamespace(status="passed", total_rounds=1),
                SimpleNamespace(status="failed", total_rounds=0),
                SimpleNamespace(status="escalated", total_rounds=3),
            ]
        ),
        _ExecuteResult(scalar_value=4),
        _ExecuteResult(scalar_value=0),
        _ExecuteResult(scalar_value=None),
    ])

    async def execute(_query):
        return next(results)

    session.execute = execute

    metrics = await get_summary_metrics(session)  # type: ignore[arg-type]

    assert metrics.total_projects == 1
    assert metrics.completed_runs == 2
    assert metrics.deterministic_failures == 4
    assert metrics.overall_pass_rate == 0.5
