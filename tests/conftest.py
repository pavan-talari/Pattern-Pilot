"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pattern_pilot.connectors.filesystem import FilesystemConnector
from pattern_pilot.core.contracts import (
    ContextBundle,
    DeterministicResult,
    Finding,
    FindingTier,
    ReviewProfile,
    ReviewRoundResult,
    Verdict,
)


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project directory with basic structure."""
    os.system(f"git init {tmp_path} --quiet 2>/dev/null")

    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / "utils.py").write_text("def add(a, b):\n    return a + b\n")

    gov = tmp_path / "governance"
    gov.mkdir()
    (gov / "rules.md").write_text("# Rules\n- No print statements in production code\n")

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")

    os.system(f"cd {tmp_path} && git add -A && git commit -m 'init' --quiet 2>/dev/null")

    return tmp_path


@pytest.fixture
def filesystem_connector(tmp_project: Path) -> FilesystemConnector:
    return FilesystemConnector(repo_path=str(tmp_project))


@pytest.fixture
def sample_findings() -> list[Finding]:
    return [
        Finding(
            tier=FindingTier.BLOCKING,
            category="security",
            file_path="main.py",
            line_start=1,
            message="Print statement in production code",
            suggestion="Use logging instead",
            autofix_safe=True,
        ),
        Finding(
            tier=FindingTier.RECOMMENDED_AUTOFIX,
            category="style",
            file_path="utils.py",
            line_start=1,
            message="Missing type hints",
            suggestion="Add type annotations",
            autofix_safe=True,
        ),
        Finding(
            tier=FindingTier.ADVISORY,
            category="documentation",
            file_path="utils.py",
            message="Consider adding docstring",
        ),
    ]


@pytest.fixture
def passing_det_results() -> list[DeterministicResult]:
    return [
        DeterministicResult(check_name="lint", passed=True, duration_ms=120),
        DeterministicResult(check_name="typecheck", passed=True, duration_ms=450),
        DeterministicResult(check_name="tests", passed=True, duration_ms=800),
    ]


@pytest.fixture
def sample_round_result(sample_findings: list[Finding]) -> ReviewRoundResult:
    return ReviewRoundResult(
        round_number=1,
        verdict=Verdict.BLOCKING,
        findings=sample_findings,
        model_used="gpt-4o",
        tokens_in=1500,
        tokens_out=800,
        cost_usd=0.012,
        duration_ms=3200,
    )


@pytest.fixture
def sample_context_bundle() -> ContextBundle:
    return ContextBundle(
        project_name="test-project",
        task_ref="TEST-001",
        review_profile=ReviewProfile.STANDARD,
        files_changed={"main.py": "print('hello')\n"},
        governance_rules={"governance/rules.md": "# Rules\n- No print statements\n"},
    )
