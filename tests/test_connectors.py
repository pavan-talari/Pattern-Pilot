"""Tests for filesystem connector."""

from __future__ import annotations

import os

import pytest

from pattern_pilot.connectors.filesystem import FilesystemConnector
from pattern_pilot.core.contracts import ConnectorCapability


class TestFilesystemConnector:
    def test_capabilities(self, filesystem_connector: FilesystemConnector):
        info = filesystem_connector.get_info()
        assert ConnectorCapability.GOVERNANCE_READ in info.capabilities
        assert ConnectorCapability.GIT_CONTEXT_READ in info.capabilities
        assert info.connector_type == "filesystem"

    def test_has_capability(self, filesystem_connector: FilesystemConnector):
        assert filesystem_connector.has_capability(ConnectorCapability.GOVERNANCE_READ)
        assert not filesystem_connector.has_capability(ConnectorCapability.TASK_READ)

    @pytest.mark.asyncio
    async def test_read_file(self, filesystem_connector: FilesystemConnector):
        content = await filesystem_connector.read_file("main.py")
        assert "print" in content

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, filesystem_connector: FilesystemConnector):
        with pytest.raises(FileNotFoundError):
            await filesystem_connector.read_file("nonexistent.py")

    @pytest.mark.asyncio
    async def test_read_governance(self, filesystem_connector: FilesystemConnector):
        result = await filesystem_connector.read_governance(["governance"])
        assert len(result) > 0
        # Should have found governance/rules.md
        gov_paths = list(result.keys())
        assert any("rules.md" in p for p in gov_paths)

    @pytest.mark.asyncio
    async def test_read_governance_single_file(
        self, filesystem_connector: FilesystemConnector
    ):
        result = await filesystem_connector.read_governance(["governance/rules.md"])
        assert "governance/rules.md" in result
        assert "No print statements" in result["governance/rules.md"]

    def test_content_hash(self):
        h1 = FilesystemConnector.content_hash("hello")
        h2 = FilesystemConnector.content_hash("hello")
        h3 = FilesystemConnector.content_hash("world")
        assert h1 == h2
        assert h1 != h3

    @pytest.mark.asyncio
    async def test_list_files(self, filesystem_connector: FilesystemConnector):
        files = await filesystem_connector.list_files(".", extensions=[".py"])
        assert "main.py" in files
        assert "utils.py" in files

    @pytest.mark.asyncio
    async def test_optional_methods_return_defaults(
        self, filesystem_connector: FilesystemConnector
    ):
        assert await filesystem_connector.read_task("TASK-1") is None
        assert await filesystem_connector.read_dependencies(["main.py"]) == {}
        assert await filesystem_connector.read_test_config() == {}

    @pytest.mark.asyncio
    async def test_list_changed_files_from_git_diff(
        self, filesystem_connector: FilesystemConnector
    ):
        repo = filesystem_connector.repo_path
        with open(f"{repo}/main.py", "a", encoding="utf-8") as handle:
            handle.write("print('updated')\n")

        files = await filesystem_connector.list_changed_files(
            diff_base="HEAD",
            diff_scope="unstaged",
        )
        assert "main.py" in files

    @pytest.mark.asyncio
    async def test_get_file_diff_supports_staged_scope(
        self, filesystem_connector: FilesystemConnector
    ):
        repo = filesystem_connector.repo_path
        file_path = f"{repo}/utils.py"
        with open(file_path, "a", encoding="utf-8") as handle:
            handle.write("def mul(a, b):\n    return a * b\n")
        os.system(f"cd {repo} && git add utils.py")

        diff = await filesystem_connector.get_file_diff(
            "utils.py",
            diff_base="HEAD",
            diff_scope="staged",
        )
        assert diff is not None
        assert "+def mul(a, b):" in diff

    @pytest.mark.asyncio
    async def test_list_changed_files_includes_untracked_files(
        self, filesystem_connector: FilesystemConnector
    ):
        repo = filesystem_connector.repo_path
        with open(f"{repo}/new_untracked.py", "w", encoding="utf-8") as handle:
            handle.write("print('new file')\n")

        files = await filesystem_connector.list_changed_files(
            diff_base="HEAD",
            diff_scope="unstaged",
        )
        assert "new_untracked.py" in files

    @pytest.mark.asyncio
    async def test_staged_scope_works_without_head_commit(self, tmp_path):
        repo = str(tmp_path / "no-commit-repo")
        os.system(f"git init {repo} --quiet 2>/dev/null")
        with open(f"{repo}/alpha.py", "w", encoding="utf-8") as handle:
            handle.write("print('alpha')\n")
        os.system(f"cd {repo} && git add alpha.py")

        connector = FilesystemConnector(repo_path=repo)
        files = await connector.list_changed_files(
            diff_base="HEAD",
            diff_scope="staged",
        )
        assert "alpha.py" in files

        diff = await connector.get_file_diff(
            "alpha.py",
            diff_base="HEAD",
            diff_scope="staged",
        )
        assert diff is not None
        assert "+print('alpha')" in diff
