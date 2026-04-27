"""Deterministic check runner — lint, typecheck, tests.

Runs BEFORE the LLM reviewer. If deterministic checks fail,
no LLM round is consumed — the submission is returned immediately.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pattern_pilot.core.contracts import DeterministicResult

logger = logging.getLogger(__name__)


@dataclass
class CheckConfig:
    """Configuration for a single deterministic check."""

    name: str
    command: list[str]
    working_dir: str
    timeout_seconds: int = 120
    enabled: bool = True


class CheckRunner:
    """Executes deterministic checks against a target project.

    Checks are discovered from project config during onboarding.
    Pattern Pilot does not assume which checks exist — the project
    declares them (or the scanner infers sensible defaults).
    """

    def __init__(
        self,
        working_dir: str,
        checks: list[CheckConfig] | None = None,
        files_changed: list[str] | None = None,
    ) -> None:
        self.working_dir = working_dir
        self.files_changed = files_changed or []
        self.checks = checks or self._default_checks()

    def _default_checks(self) -> list[CheckConfig]:
        """Sensible defaults — can be overridden per project."""
        python_targets = self._python_targets()
        if self.files_changed:
            if not python_targets:
                return []
            checks = [
                CheckConfig(
                    name="lint",
                    command=["ruff", "check", *python_targets],
                    working_dir=self.working_dir,
                ),
                CheckConfig(
                    name="typecheck",
                    command=["mypy", *python_targets],
                    working_dir=self.working_dir,
                ),
            ]
            test_targets = [path for path in python_targets if self._is_test_path(path)]
            if test_targets:
                checks.append(
                    CheckConfig(
                        name="tests",
                        command=["pytest", "--tb=short", "-q", *test_targets],
                        working_dir=self.working_dir,
                        timeout_seconds=300,
                    )
                )
            return checks

        return [
            CheckConfig(
                name="lint",
                command=["ruff", "check", "."],
                working_dir=self.working_dir,
            ),
            CheckConfig(
                name="typecheck",
                command=["mypy", "."],
                working_dir=self.working_dir,
            ),
            CheckConfig(
                name="tests",
                command=["pytest", "--tb=short", "-q"],
                working_dir=self.working_dir,
                timeout_seconds=300,
            ),
        ]

    def _python_targets(self) -> list[str]:
        """Return changed Python files to use for scoped deterministic checks."""
        seen: set[str] = set()
        targets: list[str] = []
        for path in self.files_changed:
            normalized = path.strip()
            if (
                normalized
                and normalized.endswith((".py", ".pyi"))
                and normalized not in seen
            ):
                seen.add(normalized)
                targets.append(normalized)
        return targets

    @staticmethod
    def _is_test_path(path: str) -> bool:
        """Detect whether a changed file is a pytest target."""
        normalized = Path(path)
        parts = {part.lower() for part in normalized.parts}
        name = normalized.name.lower()
        return (
            "tests" in parts
            or name.startswith("test_")
            or name.endswith("_test.py")
        )

    async def run_all(self) -> list[DeterministicResult]:
        """Run all enabled checks sequentially, return results."""
        results: list[DeterministicResult] = []
        for check in self.checks:
            if not check.enabled:
                continue
            result = await self._run_check(check)
            results.append(result)
            logger.info(
                "Check %s: %s (%.0fms)",
                check.name,
                "PASS" if result.passed else "FAIL",
                result.duration_ms,
            )
        return results

    async def run_single(self, check_name: str) -> DeterministicResult | None:
        """Run a single check by name."""
        for check in self.checks:
            if check.name == check_name:
                return await self._run_check(check)
        return None

    async def _run_check(self, check: CheckConfig) -> DeterministicResult:
        """Execute one check and capture output."""
        start = time.monotonic()
        command = self._resolve_command(check.command)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=check.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=check.timeout_seconds
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                elapsed = int((time.monotonic() - start) * 1000)
                return DeterministicResult(
                    check_name=check.name,
                    passed=False,
                    output=f"Timed out after {check.timeout_seconds}s",
                    duration_ms=elapsed,
                )

            elapsed = int((time.monotonic() - start) * 1000)
            output_text = stdout.decode(errors="replace") if stdout else ""
            # Truncate huge outputs
            if len(output_text) > 10_000:
                output_text = output_text[:10_000] + "\n... (truncated)"

            return DeterministicResult(
                check_name=check.name,
                passed=(proc.returncode == 0),
                output=output_text,
                duration_ms=elapsed,
            )
        except FileNotFoundError:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.warning(
                "Check %s failed — tool not found: %s",
                check.name,
                check.command[0],
            )
            return DeterministicResult(
                check_name=check.name,
                passed=False,
                output=f"Command not found: {check.command[0]}",
                duration_ms=elapsed,
            )

    def _resolve_command(self, command: list[str]) -> list[str]:
        """Resolve a check command against PATH, project venvs, or runtime venv bins."""
        executable = command[0]
        if Path(executable).is_absolute() or "/" in executable:
            return command
        if shutil.which(executable):
            return command

        working_dir = Path(self.working_dir)
        for venv_dir in (".venv", "venv"):
            candidate = working_dir / venv_dir / "bin" / executable
            if candidate.is_file():
                return [str(candidate), *command[1:]]

        runtime_candidate = Path(sys.executable).resolve().parent / executable
        if runtime_candidate.is_file():
            return [str(runtime_candidate), *command[1:]]

        return command

    @staticmethod
    def all_passed(results: list[DeterministicResult]) -> bool:
        """Check if all deterministic results passed."""
        return all(r.passed for r in results)
