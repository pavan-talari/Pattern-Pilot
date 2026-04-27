from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from pattern_pilot.db import models
from pattern_pilot.mcp_server import _handle_list_runs, list_tools


class _ExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[tuple[models.ReviewRun, str]]) -> None:
        self._rows = rows
        self.queries: list[Any] = []

    async def execute(self, query: Any) -> _ExecuteResult:
        self.queries.append(query)
        return _ExecuteResult(self._rows)


class _SessionContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_list_tools_includes_list_runs() -> None:
    tools = await list_tools()
    names = {tool.name for tool in tools}
    assert "list_runs" in names


@pytest.mark.asyncio
async def test_submit_tool_exposes_git_diff_options() -> None:
    tools = await list_tools()
    submit_tool = next(tool for tool in tools if tool.name == "submit_for_review")
    properties = submit_tool.inputSchema["properties"]
    required = submit_tool.inputSchema["required"]

    assert "use_git_diff" in properties
    assert "diff_base" in properties
    assert "diff_scope" in properties
    assert "files_changed" not in required


@pytest.mark.asyncio
async def test_handle_list_runs_returns_task_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = models.ReviewRun(
        id="run-123",
        project_id="project-1",
        task_ref="TASK-716",
        task_id="TASK-716",
        status="passed",
        verdict="pass",
        review_profile="standard",
        total_rounds=2,
        total_submissions=2,
        created_at=datetime(2026, 4, 19, 9, 30, 0),
    )
    session = _FakeSession([(run, "story-engine")])
    monkeypatch.setattr(
        "pattern_pilot.mcp_server.AsyncSessionLocal",
        lambda: _SessionContext(session),
    )

    content = await _handle_list_runs({"task_id": "TASK-716", "project_name": "story-engine"})
    text = content[0].text

    assert "Run history for TASK-716" in text
    assert "run-123" in text
    assert "story-engine" in text
    assert "rounds=2" in text
    assert "review_runs.task_id" in str(session.queries[0])


@pytest.mark.asyncio
async def test_handle_list_runs_returns_empty_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession([])
    monkeypatch.setattr(
        "pattern_pilot.mcp_server.AsyncSessionLocal",
        lambda: _SessionContext(session),
    )

    content = await _handle_list_runs({"task_id": "TASK-999"})

    assert "No runs found" in content[0].text
