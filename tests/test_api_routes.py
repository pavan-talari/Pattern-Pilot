"""Focused route helper tests."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from pattern_pilot.api.routes.config import get_provider_config
from pattern_pilot.api.routes.projects import (
    ProjectCreate,
    ProjectModelUpdate,
    _resolve_repo_path_for_scan,
    _scan_project_or_raise,
    delete_project,
    list_projects,
    onboard_project,
    update_project_model,
)
from pattern_pilot.api.routes.reviews import (
    get_review,
    get_rounds,
    get_submissions,
    list_runs,
    list_reviews,
)
from pattern_pilot.db import models
from pattern_pilot.scanner.project_scanner import ProjectScanError, ProjectScanner, ScanResult


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _ExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, results: list[_ExecuteResult]) -> None:
        self._results = results
        self.execute_count = 0
        self.queries: list[Any] = []

    async def execute(self, query: Any) -> _ExecuteResult:
        self.execute_count += 1
        self.queries.append(query)
        return self._results.pop(0)


class _ProjectSession:
    def __init__(self, project: models.Project | None = None) -> None:
        self.project = project
        self.added: models.Project | None = None
        self.queries: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added = obj

    async def execute(self, query: Any) -> _ExecuteResult:
        self.queries.append(query)
        return _ExecuteResult([])

    async def get(self, model: Any, project_id: str) -> models.Project | None:
        return self.project

    async def flush(self) -> None:
        return None


class _ReviewDetailSession:
    def __init__(
        self,
        run: models.ReviewRun,
        submission: models.ReviewSubmission | None,
        event: models.EventLog | None = None,
    ) -> None:
        self.run = run
        self.submission = submission
        self.event = event
        self.execute_count = 0

    async def get(self, model: Any, run_id: str) -> models.ReviewRun | None:
        return self.run if run_id == self.run.id else None

    async def execute(self, query: Any) -> _ExecuteResult:
        self.execute_count += 1
        if self.execute_count == 1:
            rows = [self.submission] if self.submission is not None else []
        else:
            rows = [self.event] if self.event is not None else []
        return _ExecuteResult(rows)


def _round(round_id: str, round_number: int) -> models.ReviewRound:
    return models.ReviewRound(
        id=round_id,
        run_id="run-1",
        round_number=round_number,
        verdict="pass_with_advisories",
        model_used="gpt-5.4",
        tokens_in=10,
        tokens_out=5,
        duration_ms=100,
    )


def _submission(submission_id: str, number: int) -> models.ReviewSubmission:
    return models.ReviewSubmission(
        id=submission_id,
        run_id="run-1",
        submission_number=number,
        diff_hash=f"hash-{number}",
        files_changed=["backend/app/main.py"],
        deterministic_results=[
            {
                "check_name": "lint",
                "passed": False,
                "output": "I001 import block is un-sorted",
                "duration_ms": 42,
            }
        ],
        deterministic_passed=False,
        progressed_to_llm=False,
        created_at=datetime(2026, 4, 18, 12, 0, 0),
    )


def _event(
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: str = "run-1",
) -> models.EventLog:
    return models.EventLog(
        id=f"event-{event_type}",
        project_id="project-1",
        run_id=run_id,
        event_type=event_type,
        payload=payload,
        created_at=datetime(2026, 4, 18, 12, 0, 30),
    )


def _run_response(
    run_id: str,
    *,
    status: str = "failed",
    verdict: str | None = "failed",
    total_rounds: int = 0,
) -> models.ReviewRun:
    return models.ReviewRun(
        id=run_id,
        project_id="project-1",
        task_ref="TASK-716",
        status=status,
        verdict=verdict,
        review_profile="standard",
        total_rounds=total_rounds,
        total_submissions=1,
        started_at=datetime(2026, 4, 18, 12, 0, 0),
        completed_at=datetime(2026, 4, 18, 12, 5, 0),
    )


def test_scan_project_or_raise_distinguishes_missing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing(self: ProjectScanner) -> None:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(ProjectScanner, "scan", raise_missing)

    with pytest.raises(HTTPException) as exc_info:
        _scan_project_or_raise("/missing/project")

    assert exc_info.value.status_code == 400
    assert "Repo path not found" in str(exc_info.value.detail)


def test_scan_project_or_raise_reports_scanner_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_scan_error(self: ProjectScanner) -> None:
        raise ProjectScanError("bad metadata")

    monkeypatch.setattr(ProjectScanner, "scan", raise_scan_error)

    with pytest.raises(HTTPException) as exc_info:
        _scan_project_or_raise("/existing/project")

    assert exc_info.value.status_code == 422
    assert "could not be scanned" in str(exc_info.value.detail)
    assert "bad metadata" in str(exc_info.value.detail)


def test_scan_project_or_raise_reports_unexpected_scanner_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_bug(self: ProjectScanner) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(ProjectScanner, "scan", raise_bug)

    with pytest.raises(HTTPException) as exc_info:
        _scan_project_or_raise("/existing/project")

    assert exc_info.value.status_code == 500
    assert "unexpectedly" in str(exc_info.value.detail)


def test_resolve_repo_path_for_scan_uses_config_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_is_dir(self: Any) -> bool:
        return str(self) not in {
            "/Users/pavanktalari/Projects/Amitara/story-engine",
            "/projects",
            "/projects/story-engine",
        }

    monkeypatch.setattr(
        "pattern_pilot.api.routes.projects.get_settings",
        lambda: SimpleNamespace(
            resolve_repo_path=lambda repo_path: repo_path.replace(
                "/Users/pavanktalari/Projects/Amitara",
                "/projects",
            )
        ),
    )
    monkeypatch.setattr("pathlib.Path.is_dir", fake_is_dir)

    resolved = _resolve_repo_path_for_scan(
        "/Users/pavanktalari/Projects/Amitara/story-engine"
    )

    assert resolved == "/projects/story-engine"


def test_resolve_repo_path_for_scan_keeps_existing_projects_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    projects_root = tmp_path / "projects"
    repo_dir = projects_root / "story-engine"
    repo_dir.mkdir(parents=True)

    def fake_expanduser(self: Any) -> Any:
        if str(self) == "/projects/story-engine":
            return repo_dir
        return self

    monkeypatch.setattr(
        "pattern_pilot.api.routes.projects.get_settings",
        lambda: SimpleNamespace(
            resolve_repo_path=lambda repo_path: repo_path.replace(
                "/projects",
                "/Users/pavanktalari/Projects/AmiTara",
            )
        ),
    )
    monkeypatch.setattr("pathlib.Path.expanduser", fake_expanduser)

    resolved = _resolve_repo_path_for_scan("/projects/story-engine")

    assert resolved == str(repo_dir)


def test_project_scanner_detects_mixed_repo_signals_from_first_level_subdir(
    tmp_path: Any,
) -> None:
    (tmp_path / "package.json").write_text("{}")
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text("[project]\nname='story-engine'\n")
    (backend / "pytest.ini").write_text("[pytest]\n")

    result = ProjectScanner(str(tmp_path)).scan()

    assert "javascript" in result.languages
    assert "python" in result.languages
    assert "backend/pyproject.toml" in result.config_files
    assert "pytest" in result.tools


@pytest.mark.asyncio
async def test_get_rounds_uses_single_aggregate_count_query() -> None:
    session = _FakeSession([
        _ExecuteResult([_round("round-1", 1), _round("round-2", 2)]),
        _ExecuteResult([("round-1", 2)]),
    ])

    response = await get_rounds("run-1", session)  # type: ignore[arg-type]

    assert session.execute_count == 2
    assert [item.findings_count for item in response] == [2, 0]


@pytest.mark.asyncio
async def test_get_submissions_returns_deterministic_results() -> None:
    session = _FakeSession([
        _ExecuteResult([_submission("sub-1", 1), _submission("sub-2", 2)]),
    ])

    response = await get_submissions("run-1", session)  # type: ignore[arg-type]

    assert session.execute_count == 1
    assert [item.submission_number for item in response] == [1, 2]
    assert response[-1].deterministic_results[0]["check_name"] == "lint"
    assert response[-1].progressed_to_llm is False


@pytest.mark.asyncio
async def test_list_reviews_marks_deterministic_failures_separately() -> None:
    session = _FakeSession([
        _ExecuteResult([
            _run_response("run-1", status="failed", verdict="failed", total_rounds=0),
            _run_response("run-2", status="failed", verdict="failed", total_rounds=2),
        ]),
        _ExecuteResult([
            _submission("sub-1", 1),
            models.ReviewSubmission(
                id="sub-2",
                run_id="run-2",
                submission_number=1,
                diff_hash="hash-2",
                files_changed=["backend/app/main.py"],
                deterministic_results=[],
                deterministic_passed=True,
                progressed_to_llm=True,
                created_at=datetime(2026, 4, 18, 12, 1, 0),
            ),
        ]),
        _ExecuteResult([
            _event("run_failed", {"phase": "deterministic_checks"}, run_id="run-1"),
            _event("run_failed", {"phase": "worker", "error": "boom"}, run_id="run-2"),
        ]),
    ])

    response = await list_reviews("project-1", session)  # type: ignore[arg-type]

    assert response[0].failure_kind == "deterministic_checks"
    assert response[1].failure_kind is None


@pytest.mark.asyncio
async def test_get_review_preserves_detail_fields_and_failure_kind() -> None:
    run = models.ReviewRun(
        id="run-1",
        project_id="project-1",
        task_ref="TASK-716",
        status="failed",
        verdict="failed",
        review_profile="standard",
        total_rounds=0,
        total_submissions=1,
        governance_snapshot={"captured_at": "2026-04-18T12:00:00"},
        prompt_version="v1.3",
        diff_hash="hash-1",
        connector_type="filesystem",
        started_at=datetime(2026, 4, 18, 12, 0, 0),
        completed_at=datetime(2026, 4, 18, 12, 5, 0),
    )
    session = _ReviewDetailSession(
        run,
        _submission("sub-1", 1),
        _event("run_failed", {"phase": "deterministic_checks"}),
    )

    response = await get_review("run-1", session)  # type: ignore[arg-type]

    assert response.failure_kind == "deterministic_checks"
    assert response.governance_snapshot == {"captured_at": "2026-04-18T12:00:00"}
    assert response.connector_type == "filesystem"


@pytest.mark.asyncio
async def test_list_reviews_marks_reviewer_failures_separately() -> None:
    reviewer_run = _run_response("run-1", status="failed", verdict="failed", total_rounds=0)
    reviewer_error_run = _run_response(
        "run-2", status="reviewer_error", verdict=None, total_rounds=0
    )
    session = _FakeSession([
        _ExecuteResult([reviewer_run, reviewer_error_run]),
        _ExecuteResult([
            models.ReviewSubmission(
                id="sub-1",
                run_id="run-1",
                submission_number=1,
                diff_hash="hash-1",
                files_changed=["backend/app/main.py"],
                deterministic_results=[],
                deterministic_passed=True,
                progressed_to_llm=True,
                created_at=datetime(2026, 4, 18, 12, 0, 0),
            ),
            models.ReviewSubmission(
                id="sub-2",
                run_id="run-2",
                submission_number=1,
                diff_hash="hash-2",
                files_changed=["backend/app/main.py"],
                deterministic_results=[],
                deterministic_passed=True,
                progressed_to_llm=True,
                created_at=datetime(2026, 4, 18, 12, 1, 0),
            ),
        ]),
        _ExecuteResult([
            _event(
                "run_failed",
                {"phase": "reviewer", "error": "429 insufficient_quota"},
                run_id="run-1",
            ),
            _event(
                "run_reviewer_error",
                {"phase": "reviewer", "error": "429 insufficient_quota"},
                run_id="run-2",
            ),
        ]),
    ])

    response = await list_reviews("project-1", session)  # type: ignore[arg-type]

    assert response[0].failure_kind == "reviewer_infrastructure"
    assert response[0].failure_reason == "429 insufficient_quota"
    assert response[1].failure_kind == "reviewer_infrastructure"


@pytest.mark.asyncio
async def test_list_reviews_exposes_task_id_when_present() -> None:
    run = _run_response("run-1", status="passed", verdict="pass", total_rounds=1)
    run.task_id = "TASK-839"
    session = _FakeSession([
        _ExecuteResult([run]),
        _ExecuteResult([
            models.ReviewSubmission(
                id="sub-1",
                run_id="run-1",
                submission_number=1,
                diff_hash="hash-1",
                files_changed=["backend/app/main.py"],
                deterministic_results=[],
                deterministic_passed=True,
                progressed_to_llm=True,
                created_at=datetime(2026, 4, 19, 8, 30, 0),
            ),
        ]),
        _ExecuteResult([]),
    ])

    response = await list_reviews("project-1", session)  # type: ignore[arg-type]

    assert len(response) == 1
    assert response[0].task_id == "TASK-839"


@pytest.mark.asyncio
async def test_list_runs_by_task_id_returns_history_with_failure_metadata() -> None:
    run1 = _run_response("run-1", status="failed", verdict="failed", total_rounds=0)
    run1.task_id = None
    run1.task_ref = "TASK-716"
    run1.created_at = datetime(2026, 4, 19, 7, 0, 0)
    run2 = _run_response(
        "run-2", status="reviewer_error", verdict=None, total_rounds=1
    )
    run2.task_id = "TASK-716"
    run2.created_at = datetime(2026, 4, 19, 8, 0, 0)
    session = _FakeSession([
        _ExecuteResult([(run2, "story-engine"), (run1, "story-engine")]),
        _ExecuteResult([
            models.ReviewSubmission(
                id="sub-1",
                run_id="run-1",
                submission_number=1,
                diff_hash="hash-1",
                files_changed=["backend/app/main.py"],
                deterministic_results=[
                    {"check_name": "lint", "passed": False, "output": "I001"}
                ],
                deterministic_passed=False,
                progressed_to_llm=False,
                created_at=datetime(2026, 4, 19, 7, 0, 30),
            ),
            models.ReviewSubmission(
                id="sub-2",
                run_id="run-2",
                submission_number=1,
                diff_hash="hash-2",
                files_changed=["backend/app/main.py"],
                deterministic_results=[],
                deterministic_passed=True,
                progressed_to_llm=True,
                created_at=datetime(2026, 4, 19, 8, 0, 30),
            ),
        ]),
        _ExecuteResult([
            _event(
                "run_reviewer_error",
                {"phase": "reviewer", "error": "429 insufficient_quota"},
                run_id="run-2",
            ),
            _event(
                "run_failed",
                {"phase": "deterministic_checks", "error": "ruff failed"},
                run_id="run-1",
            ),
        ]),
    ])

    response = await list_runs(  # type: ignore[arg-type]
        task_id="TASK-716",
        project_name="story-engine",
        limit=25,
        session=session,
    )

    assert session.execute_count == 3
    assert "review_runs.task_id" in str(session.queries[0])
    assert "review_runs.task_ref" in str(session.queries[0])
    assert "projects.name" in str(session.queries[0])
    assert [item.id for item in response] == ["run-2", "run-1"]
    assert response[0].project_name == "story-engine"
    assert response[0].failure_kind == "reviewer_infrastructure"
    assert response[1].failure_kind == "deterministic_checks"


@pytest.mark.asyncio
async def test_get_review_includes_reviewer_failure_reason() -> None:
    run = models.ReviewRun(
        id="run-1",
        project_id="project-1",
        task_ref="TASK-716",
        status="reviewer_error",
        verdict=None,
        review_profile="standard",
        total_rounds=0,
        total_submissions=1,
        governance_snapshot={"captured_at": "2026-04-18T12:00:00"},
        prompt_version="v1.3",
        diff_hash="hash-1",
        connector_type="filesystem",
        started_at=datetime(2026, 4, 18, 12, 0, 0),
        completed_at=datetime(2026, 4, 18, 12, 5, 0),
    )
    session = _ReviewDetailSession(
        run,
        models.ReviewSubmission(
            id="sub-1",
            run_id="run-1",
            submission_number=1,
            diff_hash="hash-1",
            files_changed=["backend/app/main.py"],
            deterministic_results=[],
            deterministic_passed=True,
            progressed_to_llm=True,
            created_at=datetime(2026, 4, 18, 12, 0, 0),
        ),
        _event(
            "run_reviewer_error",
            {"phase": "reviewer", "error": "OpenAI API quota exceeded (429 insufficient_quota)"},
        ),
    )

    response = await get_review("run-1", session)  # type: ignore[arg-type]

    assert response.failure_kind == "reviewer_infrastructure"
    assert response.failure_reason == "OpenAI API quota exceeded (429 insufficient_quota)"


@pytest.mark.asyncio
async def test_onboard_project_persists_reviewer_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanned_paths: list[str] = []

    def fake_is_dir(self: Any) -> bool:
        return str(self) not in {
            "/Users/pavanktalari/Projects/Amitara/story-engine",
            "/projects",
            "/projects/story-engine",
        }

    def scan(self: ProjectScanner) -> ScanResult:
        scanned_paths.append(str(self.repo_path))
        return ScanResult(repo_path="/tmp/project")

    monkeypatch.setattr(
        "pattern_pilot.api.routes.projects.get_settings",
        lambda: SimpleNamespace(
            resolve_repo_path=lambda repo_path: repo_path.replace(
                "/Users/pavanktalari/Projects/Amitara",
                "/projects",
            )
        ),
    )
    monkeypatch.setattr("pathlib.Path.is_dir", fake_is_dir)
    monkeypatch.setattr(ProjectScanner, "scan", scan)
    session = _ProjectSession()
    repo_path = "/Users/pavanktalari/Projects/Amitara/story-engine"

    project = await onboard_project(
        ProjectCreate(
            name="demo",
            repo_path=repo_path,
            reviewer_provider="openai",
            reviewer_model="gpt-4o",
            reviewer_reasoning_effort="high",
        ),
        session,  # type: ignore[arg-type]
    )

    assert scanned_paths == ["/projects/story-engine"]
    assert project.repo_path == repo_path
    assert project.reviewer_provider == "openai"
    assert project.reviewer_model == "gpt-4o"
    assert project.reviewer_reasoning_effort == "high"


@pytest.mark.asyncio
async def test_update_project_model_updates_persisted_fields() -> None:
    project = models.Project(name="demo", repo_path="/tmp/project")
    session = _ProjectSession(project=project)

    updated = await update_project_model(
        "project-1",
        ProjectModelUpdate(
            reviewer_provider="openai",
            reviewer_model="gpt-5.4",
            reviewer_reasoning_effort="medium",
        ),
        session,  # type: ignore[arg-type]
    )

    assert updated.reviewer_provider == "openai"
    assert updated.reviewer_model == "gpt-5.4"
    assert updated.reviewer_reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_update_project_model_accepts_enabled_non_openai_provider() -> None:
    project = models.Project(name="demo", repo_path="/tmp/project")
    session = _ProjectSession(project=project)

    updated = await update_project_model(
        "project-1",
        ProjectModelUpdate(
            reviewer_provider="anthropic",
            reviewer_model="claude-sonnet-4",
            reviewer_reasoning_effort=None,
        ),
        session,  # type: ignore[arg-type]
    )

    assert updated.reviewer_provider == "anthropic"
    assert updated.reviewer_model == "claude-sonnet-4"
    assert updated.reviewer_reasoning_effort is None


@pytest.mark.asyncio
async def test_delete_project_archives_instead_of_deleting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = models.Project(name="demo", repo_path="/tmp/project")
    session = _ProjectSession(project=project)
    archived_at = datetime(2026, 4, 16, 16, 30, 0)

    monkeypatch.setattr(
        "pattern_pilot.api.routes.projects.pp_now",
        lambda: archived_at,
    )

    response = await delete_project("project-1", session)  # type: ignore[arg-type]

    assert project.archived_at == archived_at
    assert "preserved" in response["message"]


@pytest.mark.asyncio
async def test_list_projects_filters_archived_projects() -> None:
    session = _ProjectSession()

    await list_projects(session)  # type: ignore[arg-type]

    assert session.queries
    assert "archived_at IS NULL" in str(session.queries[0])


@pytest.mark.asyncio
async def test_provider_config_exposes_enabled_model_options() -> None:
    config = await get_provider_config()

    providers = {provider.id: provider for provider in config.providers}
    assert providers["openai"].available is True
    assert providers["openai"].supports_reasoning_effort is True
    assert "gpt-5.4" in providers["openai"].models
    assert "gpt-5.4-mini" in providers["openai"].models
    assert "gpt-5-mini" in providers["openai"].models
    assert providers["anthropic"].available is True
    assert providers["google"].available is True
    assert providers["perplexity"].available is True
    assert providers["anthropic"].label == "Anthropic"
    assert providers["google"].label == "Gemini"
    assert providers["perplexity"].label == "Perplexity"
