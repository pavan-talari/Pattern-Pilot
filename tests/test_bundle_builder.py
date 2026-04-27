"""Tests for context bundle builder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pattern_pilot.connectors.filesystem import FilesystemConnector
from pattern_pilot.context.bundle_builder import BundleBuilder
from pattern_pilot.core.contracts import DeterministicResult, ReviewProfile
from pattern_pilot.governance.loader import GovernanceLoader


class TestBundleBuilder:
    @pytest.mark.asyncio
    async def test_quick_profile_no_governance(self, tmp_project: Path):
        connector = FilesystemConnector(repo_path=str(tmp_project))
        gov_loader = GovernanceLoader(connector)
        builder = BundleBuilder(connector, gov_loader)

        # Create a large changed file so quick profile can prove snippet scoping.
        large_content = "\n".join([f"line {i}" for i in range(1, 401)]) + "\n"
        (tmp_project / "main.py").write_text(large_content)
        os.system(
            f"cd {tmp_project} && git add main.py && git commit -m 'baseline main' --quiet 2>/dev/null"
        )
        (tmp_project / "main.py").write_text(
            large_content.replace("line 220", "line 220 changed")
        )

        bundle = await builder.build(
            project_name="test",
            task_ref="T-1",
            files_changed=["main.py"],
            review_profile=ReviewProfile.QUICK,
            governance_paths=["governance"],
        )

        assert bundle.project_name == "test"
        assert bundle.review_profile == ReviewProfile.QUICK
        assert "main.py" in bundle.files_changed
        assert "line 220 changed" in bundle.files_changed["main.py"]
        assert "line 1\n" not in bundle.files_changed["main.py"]
        assert "## Hunk 1" in bundle.files_changed["main.py"]
        # Quick profile should NOT include governance
        assert len(bundle.governance_rules) == 0

    @pytest.mark.asyncio
    async def test_standard_profile_includes_governance(self, tmp_project: Path):
        connector = FilesystemConnector(repo_path=str(tmp_project))
        gov_loader = GovernanceLoader(connector)
        builder = BundleBuilder(connector, gov_loader)

        bundle = await builder.build(
            project_name="test",
            task_ref="T-1",
            files_changed=["main.py"],
            review_profile=ReviewProfile.STANDARD,
            governance_paths=["governance"],
        )

        assert len(bundle.governance_rules) > 0
        assert any("rules.md" in k for k in bundle.governance_rules)

    @pytest.mark.asyncio
    async def test_standard_profile_includes_imports_and_symbols(self, tmp_project: Path):
        connector = FilesystemConnector(repo_path=str(tmp_project))
        gov_loader = GovernanceLoader(connector)
        builder = BundleBuilder(connector, gov_loader)

        baseline = (
            "\n".join(
                [
                    "from dataclasses import dataclass",
                    "import json",
                    "",
                    "class Worker:",
                    "    def run(self) -> str:",
                    "        return 'ok'",
                    "",
                    "def helper() -> str:",
                    "    return 'ready'",
                    "",
                    "if __name__ == '__main__':",
                    "    print(helper())",
                ]
            )
            + "\n"
        )
        (tmp_project / "main.py").write_text(baseline)
        os.system(
            f"cd {tmp_project} && git add main.py && git commit -m 'baseline symbols' --quiet 2>/dev/null"
        )
        (tmp_project / "main.py").write_text(
            baseline.replace("return 'ready'", "return 'ready now'")
        )

        bundle = await builder.build(
            project_name="test",
            task_ref="T-1",
            files_changed=["main.py"],
            review_profile=ReviewProfile.STANDARD,
            governance_paths=[],
        )

        payload = bundle.files_changed["main.py"]
        assert "## Imports" in payload
        assert "from dataclasses import dataclass" in payload
        assert "## Symbols" in payload
        assert "class Worker:" in payload
        assert "def helper() -> str:" in payload

    @pytest.mark.asyncio
    async def test_deep_profile_keeps_full_file(self, tmp_project: Path):
        connector = FilesystemConnector(repo_path=str(tmp_project))
        gov_loader = GovernanceLoader(connector)
        builder = BundleBuilder(connector, gov_loader)

        content = "\n".join([f"line {i}" for i in range(1, 120)]) + "\n"
        (tmp_project / "main.py").write_text(content)

        bundle = await builder.build(
            project_name="test",
            task_ref="T-1",
            files_changed=["main.py"],
            review_profile=ReviewProfile.DEEP,
            governance_paths=[],
        )

        assert bundle.files_changed["main.py"] == content

    @pytest.mark.asyncio
    async def test_missing_file_handled(self, tmp_project: Path):
        connector = FilesystemConnector(repo_path=str(tmp_project))
        gov_loader = GovernanceLoader(connector)
        builder = BundleBuilder(connector, gov_loader)

        bundle = await builder.build(
            project_name="test",
            task_ref="T-1",
            files_changed=["nonexistent.py", "main.py"],
            review_profile=ReviewProfile.QUICK,
            governance_paths=[],
        )

        # Should include main.py but skip nonexistent.py
        assert "main.py" in bundle.files_changed
        assert "nonexistent.py" not in bundle.files_changed

    @pytest.mark.asyncio
    async def test_test_results_included(self, tmp_project: Path):
        connector = FilesystemConnector(repo_path=str(tmp_project))
        gov_loader = GovernanceLoader(connector)
        builder = BundleBuilder(connector, gov_loader)

        results = [
            DeterministicResult(check_name="lint", passed=True, duration_ms=100),
        ]
        bundle = await builder.build(
            project_name="test",
            task_ref="T-1",
            files_changed=["main.py"],
            review_profile=ReviewProfile.STANDARD,
            governance_paths=[],
            test_results=results,
        )

        assert len(bundle.test_results) == 1
        assert bundle.test_results[0].check_name == "lint"
