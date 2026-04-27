"""Tests for orchestrator — mock reviewer, test loop logic."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from pattern_pilot.core.contracts import (
    DeterministicResult,
    Finding,
    FindingTier,
    ReviewProfile,
    ReviewRoundResult,
    ReviewStatus,
    SubmitRequest,
    Verdict,
)
from pattern_pilot.core.orchestrator import Orchestrator
from pattern_pilot.core.reviewer import ReviewerError
from pattern_pilot.db import models


class _ProjectQueryResult:
    def scalar_one_or_none(self) -> None:
        return None


class _ProjectQuerySession:
    def __init__(self) -> None:
        self.queries: list[Any] = []

    async def execute(self, query: Any) -> _ProjectQueryResult:
        self.queries.append(query)
        return _ProjectQueryResult()


class _ExecuteRoundSession:
    def __init__(self, project: models.Project) -> None:
        self.project = project
        self.commit_count = 0
        self.flush_count = 0

    async def get(self, model: Any, project_id: str) -> models.Project:
        return self.project

    async def commit(self) -> None:
        self.commit_count += 1

    async def flush(self) -> None:
        self.flush_count += 1


class _StoreDouble:
    def __init__(self) -> None:
        self.logged_events: list[tuple[str | None, str | None, str, dict[str, Any]]] = []
        self.recorded_submissions = 0
        self.completed_runs: list[tuple[str, str | None]] = []

    async def record_submission(self, **kwargs: Any) -> None:
        self.recorded_submissions += 1

    async def complete_run(
        self,
        run: models.ReviewRun,
        status: ReviewStatus,
        verdict: Verdict | None = None,
    ) -> None:
        run.status = status.value
        run.verdict = verdict.value if verdict else None
        self.completed_runs.append((status.value, run.verdict))

    async def _log_event(
        self,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.logged_events.append((project_id, run_id, event_type, payload))


class TestOrchestratorLogic:
    """Unit tests for orchestrator decision logic (no DB required)."""

    def test_submit_request_defaults(self):
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            files_changed=["main.py"],
        )
        assert req.review_profile == ReviewProfile.STANDARD

    def test_verdict_blocking_with_blocking_findings(self):
        """Verify that blocking findings produce BLOCKING verdict."""
        findings = [
            Finding(
                tier=FindingTier.BLOCKING,
                category="test",
                file_path="a.py",
                message="bug",
            )
        ]
        has_blocking = any(f.tier == FindingTier.BLOCKING for f in findings)
        assert has_blocking is True

    def test_verdict_pass_with_no_findings(self):
        """No findings should mean PASS."""
        findings: list[Finding] = []
        has_blocking = any(
            f.tier in (FindingTier.BLOCKING, FindingTier.RECOMMENDED_AUTOFIX)
            for f in findings
        )
        has_advisories = any(
            f.tier in (FindingTier.RECOMMENDED_REVIEW, FindingTier.ADVISORY)
            for f in findings
        )
        assert has_blocking is False
        assert has_advisories is False

    def test_verdict_pass_with_advisories(self):
        """Advisory-only findings should produce PASS_WITH_ADVISORIES."""
        findings = [
            Finding(
                tier=FindingTier.ADVISORY,
                category="docs",
                file_path="b.py",
                message="add docstring",
            )
        ]
        has_blocking = any(
            f.tier in (FindingTier.BLOCKING, FindingTier.RECOMMENDED_AUTOFIX)
            for f in findings
        )
        has_advisories = any(
            f.tier in (FindingTier.RECOMMENDED_REVIEW, FindingTier.ADVISORY)
            for f in findings
        )
        assert has_blocking is False
        assert has_advisories is True

    def test_round_result_serialization(self):
        result = ReviewRoundResult(
            round_number=1,
            verdict=Verdict.PASS,
            findings=[],
            model_used="gpt-4o",
            tokens_in=500,
            tokens_out=200,
            cost_usd=0.003,
            duration_ms=1500,
        )
        data = result.model_dump()
        assert data["round_number"] == 1
        assert data["verdict"] == "pass"

    def test_build_reviewer_uses_project_model(self):
        orchestrator = object.__new__(Orchestrator)
        orchestrator.settings = SimpleNamespace(
            openai_default_provider="openai",
            openai_model="gpt-5.4",
            openai_reasoning_effort="medium",
            reviewer_default_model=lambda provider: "gpt-5.4",
        )
        project = SimpleNamespace(
            reviewer_provider="openai",
            reviewer_model="gpt-4o",
            reviewer_reasoning_effort="high",
        )

        with patch("pattern_pilot.core.orchestrator.Reviewer") as mock_reviewer:
            orchestrator._build_reviewer(project)  # type: ignore[arg-type]

        mock_reviewer.assert_called_once_with(
            provider="openai",
            model="gpt-4o",
            reasoning_effort="high",
        )

    def test_build_reviewer_falls_back_to_settings(self):
        orchestrator = object.__new__(Orchestrator)
        orchestrator.settings = SimpleNamespace(
            openai_default_provider="openai",
            openai_model="gpt-5.4",
            openai_reasoning_effort="medium",
            reviewer_default_model=lambda provider: "gpt-5.4",
        )
        project = SimpleNamespace(
            reviewer_provider=None,
            reviewer_model=None,
            reviewer_reasoning_effort=None,
        )

        with patch("pattern_pilot.core.orchestrator.Reviewer") as mock_reviewer:
            orchestrator._build_reviewer(project)  # type: ignore[arg-type]

        mock_reviewer.assert_called_once_with(
            provider="openai",
            model="gpt-5.4",
            reasoning_effort="medium",
        )

    def test_build_reviewer_uses_provider_specific_default_model(self):
        orchestrator = object.__new__(Orchestrator)
        orchestrator.settings = SimpleNamespace(
            openai_default_provider="openai",
            openai_reasoning_effort="medium",
            reviewer_default_model=lambda provider: (
                "claude-sonnet-4-20250514"
                if provider == "anthropic"
                else "gpt-5.4"
            ),
        )
        project = SimpleNamespace(
            reviewer_provider="anthropic",
            reviewer_model=None,
            reviewer_reasoning_effort=None,
        )

        with patch("pattern_pilot.core.orchestrator.Reviewer") as mock_reviewer:
            orchestrator._build_reviewer(project)  # type: ignore[arg-type]

        mock_reviewer.assert_called_once_with(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            reasoning_effort="medium",
        )

    @pytest.mark.asyncio
    async def test_load_project_filters_archived_projects(self):
        session = _ProjectQuerySession()
        orchestrator = object.__new__(Orchestrator)
        orchestrator.session = session

        await orchestrator._load_project("story-engine")

        assert session.queries
        assert "archived_at IS NULL" in str(session.queries[0])

    @pytest.mark.asyncio
    async def test_resolve_files_changed_respects_git_diff_toggle(self):
        orchestrator = object.__new__(Orchestrator)

        class _Connector:
            async def list_changed_files(
                self,
                diff_base: str = "HEAD",
                diff_scope: str = "unstaged",
            ) -> list[str]:
                assert diff_base == "HEAD"
                assert diff_scope == "unstaged"
                return ["a.py", "b.py"]

        connector = _Connector()

        explicit = await orchestrator._resolve_files_changed(
            connector=connector,  # type: ignore[arg-type]
            files_changed=["manual.py"],
            use_git_diff=True,
            diff_base="HEAD",
            diff_scope="unstaged",
        )
        assert explicit == ["manual.py"]

        discovered = await orchestrator._resolve_files_changed(
            connector=connector,  # type: ignore[arg-type]
            files_changed=[],
            use_git_diff=True,
            diff_base="HEAD",
            diff_scope="unstaged",
        )
        assert discovered == ["a.py", "b.py"]

        legacy = await orchestrator._resolve_files_changed(
            connector=connector,  # type: ignore[arg-type]
            files_changed=[],
            use_git_diff=False,
            diff_base="HEAD",
            diff_scope="unstaged",
        )
        assert legacy == []

    @pytest.mark.asyncio
    async def test_execute_round_logs_deterministic_failures(self):
        project = models.Project(name="story-engine", repo_path="/tmp/story-engine")
        run = models.ReviewRun(
            id="run-1",
            project_id="project-1",
            task_ref="TASK-716",
            status=ReviewStatus.RUNNING.value,
            total_rounds=0,
            total_submissions=0,
        )
        session = _ExecuteRoundSession(project)
        store = _StoreDouble()
        orchestrator = object.__new__(Orchestrator)
        orchestrator.session = session
        orchestrator.store = store
        orchestrator.settings = SimpleNamespace(
            resolve_repo_path=lambda repo_path: repo_path,
            pp_max_rounds=3,
        )

        async def fake_load_run_for_update(run_id: str) -> models.ReviewRun:
            return run

        orchestrator._load_run_for_update = fake_load_run_for_update  # type: ignore[method-assign]
        orchestrator._build_connector = lambda project: object()  # type: ignore[method-assign]

        failed_results = [
            DeterministicResult(
                check_name="lint",
                passed=False,
                output="Command not found: ruff",
                duration_ms=5,
            )
        ]

        async def fake_run_all(self: Any) -> list[DeterministicResult]:
            return failed_results

        with patch("pattern_pilot.core.orchestrator.CheckRunner.run_all", fake_run_all):
            response = await orchestrator.execute_round(
                run_id="run-1",
                files_changed=["backend/app/main.py"],
                review_profile=ReviewProfile.STANDARD,
            )

        assert response.status == ReviewStatus.FAILED
        assert store.recorded_submissions == 1
        assert store.logged_events == [
            (
                "project-1",
                "run-1",
                "run_failed",
                {
                    "phase": "deterministic_checks",
                    "checks": [failed_results[0].model_dump()],
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_execute_round_marks_round_limit_before_escalation(self):
        project = models.Project(name="story-engine", repo_path="/tmp/story-engine")
        run = models.ReviewRun(
            id="run-1",
            project_id="project-1",
            task_ref="TASK-716",
            status=ReviewStatus.RUNNING.value,
            total_rounds=3,
            total_submissions=3,
        )
        session = _ExecuteRoundSession(project)
        store = _StoreDouble()
        orchestrator = object.__new__(Orchestrator)
        orchestrator.session = session
        orchestrator.store = store
        orchestrator.settings = SimpleNamespace(
            resolve_repo_path=lambda repo_path: repo_path,
            pp_max_rounds=3,
        )

        async def fake_load_run_for_update(run_id: str) -> models.ReviewRun:
            return run

        orchestrator._load_run_for_update = fake_load_run_for_update  # type: ignore[method-assign]
        orchestrator._build_connector = lambda project: object()  # type: ignore[method-assign]

        passed_results = [
            DeterministicResult(
                check_name="lint",
                passed=True,
                output="",
                duration_ms=5,
            )
        ]

        async def fake_run_all(self: Any) -> list[DeterministicResult]:
            return passed_results

        with patch("pattern_pilot.core.orchestrator.CheckRunner.run_all", fake_run_all):
            response = await orchestrator.execute_round(
                run_id="run-1",
                files_changed=["backend/app/main.py"],
                review_profile=ReviewProfile.STANDARD,
            )

        assert response.status == ReviewStatus.ESCALATED
        assert response.round_number == 3
        assert "No new LLM round was run" in response.message
        assert store.recorded_submissions == 1
        assert store.completed_runs == [("escalated", "requires_human_review")]
        assert store.logged_events == [
            (
                "project-1",
                "run-1",
                "run_round_limit_reached",
                {
                    "max_rounds": 3,
                    "last_completed_round": 3,
                    "review_attempted": False,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_execute_round_marks_reviewer_error_as_retryable(self):
        project = models.Project(
            name="story-engine",
            repo_path="/tmp/story-engine",
            connector_type="filesystem",
            completion_gates={},
        )
        run = models.ReviewRun(
            id="run-1",
            project_id="project-1",
            task_ref="TASK-716",
            status=ReviewStatus.RUNNING.value,
            total_rounds=0,
            total_submissions=0,
            governance_snapshot={},
        )
        session = _ExecuteRoundSession(project)
        store = _StoreDouble()
        orchestrator = object.__new__(Orchestrator)
        orchestrator.session = session
        orchestrator.store = store
        orchestrator.settings = SimpleNamespace(
            resolve_repo_path=lambda repo_path: repo_path,
            pp_max_rounds=3,
            pp_prompt_version="v1.0",
        )

        async def fake_load_run_for_update(run_id: str) -> models.ReviewRun:
            return run

        class _HealthyConnector:
            async def check_health(self, timeout: float = 10.0):
                return True, "ok"

            def get_info(self):
                return SimpleNamespace(capabilities=[])

        orchestrator._load_run_for_update = fake_load_run_for_update  # type: ignore[method-assign]
        orchestrator._build_connector = lambda project: _HealthyConnector()  # type: ignore[method-assign]

        passed_results = [
            DeterministicResult(
                check_name="lint",
                passed=True,
                output="",
                duration_ms=5,
            )
        ]

        async def fake_run_all(self: Any) -> list[DeterministicResult]:
            return passed_results

        async def fake_build(*args: Any, **kwargs: Any):
            return SimpleNamespace(
                files_changed={"backend/app/main.py": "print('ok')\n"},
                unified_diffs={},
                task_id=None,
                decision_id=None,
                attempt_number=None,
                decision_summary=None,
                task_objective=None,
                acceptance_criteria=[],
                known_exceptions=[],
                waived_findings=[],
                prior_round_findings=[],
                prior_round_number=None,
                diff_hash="",
            )

        class _ReviewerDouble:
            async def review(self, bundle: Any):
                raise ReviewerError(
                    "OpenAI reviewer unavailable after 3 attempts. RuntimeError: upstream timeout"
                )

        orchestrator._build_reviewer = lambda project: _ReviewerDouble()  # type: ignore[method-assign]

        with patch(
            "pattern_pilot.core.orchestrator.CheckRunner.run_all", fake_run_all
        ), patch("pattern_pilot.core.orchestrator.BundleBuilder.build", fake_build):
            response = await orchestrator.execute_round(
                run_id="run-1",
                files_changed=["backend/app/main.py"],
                review_profile=ReviewProfile.STANDARD,
            )

        assert response.status == ReviewStatus.REVIEWER_ERROR
        assert "Reviewer infrastructure was unavailable" in response.message
        assert store.recorded_submissions == 1
        assert store.logged_events == [
            (
                "project-1",
                "run-1",
                "run_reviewer_error",
                {
                    "phase": "reviewer",
                    "error": (
                        "OpenAI reviewer unavailable after 3 attempts. "
                        "RuntimeError: upstream timeout"
                    ),
                    "retryable": True,
                },
            )
        ]
        assert session.commit_count == 1
