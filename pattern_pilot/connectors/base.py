"""Abstract connector interface — how Pattern Pilot reads from target projects."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from pattern_pilot.core.contracts import ConnectorCapability


@dataclass
class ConnectorInfo:
    """Metadata about a connector instance."""

    connector_type: str
    capabilities: list[ConnectorCapability] = field(default_factory=list)
    repo_path: str = ""
    config: dict[str, Any] = field(default_factory=dict)


class BaseConnector(abc.ABC):
    """Abstract base for all project connectors.

    Connectors declare capabilities. Pattern Pilot degrades gracefully
    when a capability is not available (e.g., no task read → skip task snapshot).
    """

    def __init__(self, repo_path: str, config: dict[str, Any] | None = None) -> None:
        self.repo_path = repo_path
        self.config = config or {}

    @abc.abstractmethod
    def get_info(self) -> ConnectorInfo:
        """Return connector metadata and declared capabilities."""

    @abc.abstractmethod
    async def read_file(self, relative_path: str) -> str:
        """Read a single file from the target project."""

    @abc.abstractmethod
    async def read_changed_files(
        self, base_ref: str = "HEAD~1", head_ref: str = "HEAD"
    ) -> dict[str, str]:
        """Return changed files as {relative_path: content}."""

    @abc.abstractmethod
    async def read_governance(self, governance_paths: list[str]) -> dict[str, str]:
        """Read governance files. Returns {path: content}."""

    async def read_task(self, task_ref: str) -> dict[str, Any] | None:
        """Read task info from the project's backlog. Optional capability."""
        return None

    async def read_dependencies(self, file_paths: list[str]) -> dict[str, str]:
        """Read dependency graph for given files. Optional capability."""
        return {}

    async def get_file_diff(
        self,
        relative_path: str,
        diff_base: str = "HEAD",
        diff_scope: str = "unstaged",
    ) -> str | None:
        """Get unified diff for a single file. Optional capability."""
        return None

    async def list_changed_files(
        self,
        diff_base: str = "HEAD",
        diff_scope: str = "unstaged",
    ) -> list[str]:
        """List changed files from git diff. Optional capability."""
        return []

    async def read_test_config(self) -> dict[str, Any]:
        """Read test configuration/commands. Optional capability."""
        return {}

    def has_capability(self, cap: ConnectorCapability) -> bool:
        """Check if this connector supports a given capability."""
        return cap in self.get_info().capabilities
