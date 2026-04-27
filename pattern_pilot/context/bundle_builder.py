"""Context bundle builder — assembles diff-scoped context for the LLM reviewer.

The bundle size is controlled by the review profile:
- quick: diff hunks + local snippets, no governance or deps
- standard: diff hunks + selective snippets + governance + deps
- deep: full changed files + diff + governance + deps
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pattern_pilot.connectors.base import BaseConnector
from pattern_pilot.core.contracts import (
    ConnectorCapability,
    ContextBundle,
    DeterministicResult,
    ReviewProfile,
)
from pattern_pilot.governance.loader import GovernanceLoader

logger = logging.getLogger(__name__)

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class BundleBuilder:
    """Builds a ContextBundle scoped to the review profile."""

    def __init__(
        self,
        connector: BaseConnector,
        governance_loader: GovernanceLoader,
    ) -> None:
        self.connector = connector
        self.governance_loader = governance_loader

    async def build(
        self,
        project_name: str,
        task_ref: str,
        files_changed: list[str],
        review_profile: ReviewProfile,
        governance_paths: list[str],
        test_results: list[DeterministicResult] | None = None,
        project_metadata: dict[str, Any] | None = None,
        run_id: str = "",
        round_number: int = 1,
        diff_hash: str = "",
        governance_version: str = "",
        prompt_version: str = "",
        connector_type: str = "filesystem",
        connector_capabilities: list[str] | None = None,
        completion_gates: list[str] | None = None,
        diff_base: str = "HEAD",
        diff_scope: str = "unstaged",
    ) -> ContextBundle:
        """Assemble the context bundle."""
        bundle = ContextBundle(
            project_name=project_name,
            task_ref=task_ref,
            review_profile=review_profile,
            run_id=run_id,
            round_number=round_number,
            test_results=test_results or [],
            project_metadata=project_metadata or {},
            diff_hash=diff_hash,
            governance_version=governance_version,
            prompt_version=prompt_version,
            connector_type=connector_type,
            connector_capabilities=connector_capabilities or [],
            completion_gates=completion_gates or [],
        )

        # Read full changed files once, then project-profile decides what to send.
        full_changed_files = await self._read_changed_files(files_changed)

        # Always include unified diffs when git is available
        bundle.unified_diffs = await self._get_unified_diffs(
            files_changed,
            diff_base=diff_base,
            diff_scope=diff_scope,
        )
        bundle.files_changed = self._build_profile_file_payload(
            full_changed_files=full_changed_files,
            unified_diffs=bundle.unified_diffs,
            review_profile=review_profile,
        )

        if review_profile == ReviewProfile.QUICK:
            # Minimal — diff hunks + local snippets only
            return bundle

        # Standard and deep: add governance
        if governance_paths:
            _, governance_content = await self.governance_loader.load_with_content(
                governance_paths
            )
            bundle.governance_rules = governance_content

        if review_profile in (ReviewProfile.STANDARD, ReviewProfile.DEEP):
            # Standard + Deep: add dependency context (import targets of changed files)
            # This prevents false positives about data shapes, missing keys, etc.
            if self.connector.has_capability(ConnectorCapability.DEPENDENCY_READ):
                bundle.dependency_context = await self.connector.read_dependencies(
                    files_changed
                )

            # Import-following: parse imports from changed files, follow 1 level,
            # extract contract-relevant definitions (signatures, constants, classes)
            try:
                from pattern_pilot.context.import_follower import ImportFollower

                repo_root = getattr(self.connector, "repo_path", "")
                if repo_root and full_changed_files:
                    follower = ImportFollower(repo_root=repo_root)
                    import_context = await follower.follow(full_changed_files)
                    # Merge into dependency_context (don't overwrite existing)
                    for path, extracted in import_context.items():
                        if path not in bundle.dependency_context:
                            bundle.dependency_context[path] = extracted
            except Exception as exc:
                logger.warning("Import-following failed (non-fatal): %s", exc)

        return bundle

    async def _read_changed_files(self, file_paths: list[str]) -> dict[str, str]:
        """Read content of each changed file."""
        result: dict[str, str] = {}
        for path in file_paths:
            try:
                content = await self.connector.read_file(path)
                result[path] = content
            except FileNotFoundError:
                logger.warning("Changed file not found (deleted?): %s", path)
            except Exception as exc:
                logger.error("Error reading %s: %s", path, exc)
        return result

    async def _get_unified_diffs(
        self,
        file_paths: list[str],
        diff_base: str = "HEAD",
        diff_scope: str = "unstaged",
    ) -> dict[str, str]:
        """Get unified diffs for each changed file via git."""
        result: dict[str, str] = {}
        if not self.connector.has_capability(ConnectorCapability.GIT_CONTEXT_READ):
            return result

        for path in file_paths:
            try:
                diff = await self.connector.get_file_diff(
                    path,
                    diff_base=diff_base,
                    diff_scope=diff_scope,
                )
                if diff:
                    result[path] = diff
            except Exception as exc:
                logger.debug("Could not get diff for %s: %s", path, exc)
        return result

    def _build_profile_file_payload(
        self,
        full_changed_files: dict[str, str],
        unified_diffs: dict[str, str],
        review_profile: ReviewProfile,
    ) -> dict[str, str]:
        """Build the model payload for changed files by review profile."""
        if review_profile == ReviewProfile.DEEP:
            return full_changed_files

        # Quick keeps context tight. Standard adds a little more local context.
        context_lines = 20 if review_profile == ReviewProfile.QUICK else 40
        max_chars_per_file = 12000 if review_profile == ReviewProfile.QUICK else 22000
        payload: dict[str, str] = {}
        for path, content in full_changed_files.items():
            diff = unified_diffs.get(path)
            if not diff:
                payload[path] = self._fallback_preview(
                    content=content,
                    max_chars=max_chars_per_file,
                )
                continue
            hunk_ranges = self._extract_hunk_ranges(diff)
            if not hunk_ranges:
                payload[path] = self._fallback_preview(
                    content=content,
                    max_chars=max_chars_per_file,
                )
                continue
            payload[path] = self._render_profile_snippet(
                content=content,
                hunk_ranges=hunk_ranges,
                context_lines=context_lines,
                max_chars=max_chars_per_file,
                include_import_and_symbols=(review_profile == ReviewProfile.STANDARD),
            )
        return payload

    @staticmethod
    def _extract_hunk_ranges(diff: str) -> list[tuple[int, int]]:
        """Parse unified diff hunk ranges as 1-based line spans in the new file."""
        ranges: list[tuple[int, int]] = []
        for line in diff.splitlines():
            match = _HUNK_HEADER_RE.match(line)
            if not match:
                continue
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            if count <= 0:
                # Deletion-only hunk: anchor to the nearest new-file line.
                ranges.append((start, start))
                continue
            end = start + count - 1
            ranges.append((start, end))
        return ranges

    @staticmethod
    def _merge_ranges(
        ranges: list[tuple[int, int]],
        total_lines: int,
        context_lines: int,
    ) -> list[tuple[int, int]]:
        """Expand each changed range and merge overlaps."""
        expanded: list[tuple[int, int]] = []
        for start, end in ranges:
            left = max(1, start - context_lines)
            right = min(total_lines, end + context_lines)
            expanded.append((left, right))
        expanded.sort()
        merged: list[tuple[int, int]] = []
        for start, end in expanded:
            if not merged or start > merged[-1][1] + 1:
                merged.append((start, end))
            else:
                prev_start, prev_end = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end))
        return merged

    def _render_profile_snippet(
        self,
        content: str,
        hunk_ranges: list[tuple[int, int]],
        context_lines: int,
        max_chars: int,
        include_import_and_symbols: bool,
    ) -> str:
        """Render profile-scoped snippet sections from full file content."""
        lines = content.splitlines()
        if not lines:
            return ""
        merged_ranges = self._merge_ranges(
            ranges=hunk_ranges,
            total_lines=len(lines),
            context_lines=context_lines,
        )
        sections: list[str] = []

        for idx, (start, end) in enumerate(merged_ranges, start=1):
            snippet = "\n".join(lines[start - 1:end])
            sections.append(f"## Hunk {idx} (lines {start}-{end})\n{snippet}")

        if include_import_and_symbols:
            imports = self._extract_import_block(lines)
            if imports:
                sections.append(f"## Imports\n{imports}")
            symbols = self._extract_nearby_symbols(lines, hunk_ranges)
            if symbols:
                sections.append(f"## Symbols\n{symbols}")

        rendered = "\n\n".join(sections)
        if len(rendered) <= max_chars:
            return rendered
        return f"{rendered[:max_chars]}\n\n... (truncated)"

    @staticmethod
    def _extract_import_block(lines: list[str]) -> str:
        """Keep top-of-file imports for standard profile contract awareness."""
        imports: list[str] = []
        for raw in lines[:160]:
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if stripped.startswith("import ") or stripped.startswith("from "):
                imports.append(raw)
        return "\n".join(imports)

    @staticmethod
    def _extract_nearby_symbols(
        lines: list[str],
        hunk_ranges: list[tuple[int, int]],
    ) -> str:
        """Capture enclosing class/def signatures nearest to changed hunks."""
        symbol_lines: list[tuple[int, str]] = []
        seen: set[int] = set()
        for start, _ in hunk_ranges:
            idx = min(max(1, start), len(lines)) - 1
            for cursor in range(idx, -1, -1):
                stripped = lines[cursor].lstrip()
                if (
                    stripped.startswith("def ")
                    or stripped.startswith("async def ")
                    or stripped.startswith("class ")
                ):
                    line_number = cursor + 1
                    if line_number not in seen:
                        seen.add(line_number)
                        symbol_lines.append((line_number, lines[cursor].strip()))
                    break
        symbol_lines.sort(key=lambda item: item[0])
        return "\n".join(f"L{line_no}: {sig}" for line_no, sig in symbol_lines[:16])

    @staticmethod
    def _fallback_preview(content: str, max_chars: int) -> str:
        """Fallback preview when diff hunks are unavailable."""
        if len(content) <= max_chars:
            return content
        return f"{content[:max_chars]}\n\n... (truncated)"
