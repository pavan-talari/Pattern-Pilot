"""Governance loader — reads and versions governance files from a target project."""

from __future__ import annotations

import hashlib

from pattern_pilot.connectors.base import BaseConnector
from pattern_pilot.core.config import pp_now
from pattern_pilot.core.contracts import GovernanceSnapshot


class GovernanceLoader:
    """Loads governance files via a connector and produces versioned snapshots.

    Pattern Pilot does not dictate where governance lives — the project
    declares governance paths during onboarding, and this loader reads them.
    """

    def __init__(self, connector: BaseConnector) -> None:
        self.connector = connector

    async def load(self, governance_paths: list[str]) -> GovernanceSnapshot:
        """Read governance files and return a hashed snapshot."""
        raw_files = await self.connector.read_governance(governance_paths)

        file_hashes: dict[str, str] = {}
        for path, content in raw_files.items():
            file_hashes[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()

        return GovernanceSnapshot(
            files=file_hashes,
            captured_at=pp_now(),
        )

    async def load_with_content(
        self, governance_paths: list[str]
    ) -> tuple[GovernanceSnapshot, dict[str, str]]:
        """Load governance snapshot AND raw content (for context bundles)."""
        raw_files = await self.connector.read_governance(governance_paths)

        file_hashes: dict[str, str] = {}
        for path, content in raw_files.items():
            file_hashes[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()

        snapshot = GovernanceSnapshot(
            files=file_hashes,
            captured_at=pp_now(),
        )
        return snapshot, raw_files
