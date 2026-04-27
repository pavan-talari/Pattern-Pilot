"""Tests for deterministic check runner."""

from __future__ import annotations

import pytest

from pattern_pilot.checks.runner import CheckConfig, CheckRunner
from pattern_pilot.core.contracts import DeterministicResult


class TestCheckRunner:
    def test_all_passed_true(self):
        results = [
            DeterministicResult(check_name="lint", passed=True),
            DeterministicResult(check_name="tests", passed=True),
        ]
        assert CheckRunner.all_passed(results) is True

    def test_all_passed_false(self):
        results = [
            DeterministicResult(check_name="lint", passed=True),
            DeterministicResult(check_name="tests", passed=False, output="1 failed"),
        ]
        assert CheckRunner.all_passed(results) is False

    def test_all_passed_empty(self):
        assert CheckRunner.all_passed([]) is True

    def test_default_checks_created(self, tmp_path):
        runner = CheckRunner(working_dir=str(tmp_path))
        assert len(runner.checks) == 3
        names = [c.name for c in runner.checks]
        assert "lint" in names
        assert "typecheck" in names
        assert "tests" in names

    def test_default_checks_scope_to_changed_python_files(self, tmp_path):
        runner = CheckRunner(
            working_dir=str(tmp_path),
            files_changed=[
                "backend/app/main.py",
                "infra/runpod_startup.sh",
                "backend/app/models.py",
            ],
        )

        assert [check.name for check in runner.checks] == ["lint", "typecheck"]
        assert runner.checks[0].command == [
            "ruff",
            "check",
            "backend/app/main.py",
            "backend/app/models.py",
        ]
        assert runner.checks[1].command == [
            "mypy",
            "backend/app/main.py",
            "backend/app/models.py",
        ]

    def test_default_checks_include_pytest_only_for_changed_test_files(self, tmp_path):
        runner = CheckRunner(
            working_dir=str(tmp_path),
            files_changed=[
                "backend/tests/test_adapter.py",
                "backend/app/main.py",
            ],
        )

        assert [check.name for check in runner.checks] == ["lint", "typecheck", "tests"]
        assert runner.checks[2].command == [
            "pytest",
            "--tb=short",
            "-q",
            "backend/tests/test_adapter.py",
        ]

    def test_default_checks_skip_when_changed_files_have_no_python_targets(self, tmp_path):
        runner = CheckRunner(
            working_dir=str(tmp_path),
            files_changed=[
                "infra/runpod_startup.sh",
                "frontend/package.json",
            ],
        )

        assert runner.checks == []

    def test_custom_checks(self, tmp_path):
        custom = [
            CheckConfig(
                name="custom_lint",
                command=["echo", "ok"],
                working_dir=str(tmp_path),
            )
        ]
        runner = CheckRunner(working_dir=str(tmp_path), checks=custom)
        assert len(runner.checks) == 1
        assert runner.checks[0].name == "custom_lint"

    @pytest.mark.asyncio
    async def test_run_passing_check(self, tmp_path):
        check = CheckConfig(
            name="echo_test",
            command=["echo", "all good"],
            working_dir=str(tmp_path),
        )
        runner = CheckRunner(working_dir=str(tmp_path), checks=[check])
        results = await runner.run_all()
        assert len(results) == 1
        assert results[0].passed is True
        assert "all good" in results[0].output

    @pytest.mark.asyncio
    async def test_run_failing_check(self, tmp_path):
        check = CheckConfig(
            name="fail_test",
            command=["false"],
            working_dir=str(tmp_path),
        )
        runner = CheckRunner(working_dir=str(tmp_path), checks=[check])
        results = await runner.run_all()
        assert len(results) == 1
        assert results[0].passed is False

    @pytest.mark.asyncio
    async def test_command_not_found(self, tmp_path):
        check = CheckConfig(
            name="missing",
            command=["nonexistent_command_xyz"],
            working_dir=str(tmp_path),
        )
        runner = CheckRunner(working_dir=str(tmp_path), checks=[check])
        results = await runner.run_all()
        assert len(results) == 1
        assert results[0].passed is False
        assert "not found" in results[0].output.lower()

    @pytest.mark.asyncio
    async def test_run_check_uses_project_local_venv_tool(self, tmp_path):
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        tool = venv_bin / "fake_tool"
        tool.write_text("#!/bin/sh\necho local-venv-ok\n")
        tool.chmod(0o755)

        check = CheckConfig(
            name="local_tool",
            command=["fake_tool"],
            working_dir=str(tmp_path),
        )
        runner = CheckRunner(working_dir=str(tmp_path), checks=[check])

        results = await runner.run_all()

        assert len(results) == 1
        assert results[0].passed is True
        assert "local-venv-ok" in results[0].output

    @pytest.mark.asyncio
    async def test_run_check_uses_runtime_venv_tool_when_project_lacks_tool(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        runtime_bin = tmp_path / "runtime" / "bin"
        runtime_bin.mkdir(parents=True)
        tool = runtime_bin / "fake_runtime_tool"
        tool.write_text("#!/bin/sh\necho runtime-venv-ok\n")
        tool.chmod(0o755)

        monkeypatch.setattr("pattern_pilot.checks.runner.sys.executable", str(runtime_bin / "python"))
        check = CheckConfig(
            name="runtime_tool",
            command=["fake_runtime_tool"],
            working_dir=str(tmp_path),
        )
        runner = CheckRunner(working_dir=str(tmp_path), checks=[check])

        results = await runner.run_all()

        assert len(results) == 1
        assert results[0].passed is True
        assert "runtime-venv-ok" in results[0].output

    @pytest.mark.asyncio
    async def test_disabled_check_skipped(self, tmp_path):
        checks = [
            CheckConfig(name="enabled", command=["echo", "ok"], working_dir=str(tmp_path)),
            CheckConfig(name="disabled", command=["echo", "skip"], working_dir=str(tmp_path), enabled=False),
        ]
        runner = CheckRunner(working_dir=str(tmp_path), checks=checks)
        results = await runner.run_all()
        assert len(results) == 1
        assert results[0].check_name == "enabled"
