"""Filesystem context resolver — resolves decision and task context from markdown files.

Phase 2 of the context-based review workflow. Instead of requiring callers to send
full decision/task context in every submission, Pattern Pilot resolves it from the
target project's filesystem using stable IDs (decision_id, task_id).

Directory conventions (configurable per project):
    docs/decisions/DEC-302.md   →  decision context
    docs/tasks/TASK-653.md      →  task context

The resolver reads these files via the project connector, parses structured
markdown sections, and returns a typed context dict that the orchestrator
injects into the review bundle.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pattern_pilot.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

# Default directory conventions — overridable via project config
DEFAULT_DECISIONS_DIR = "docs/decisions"
DEFAULT_TASKS_DIR = "docs/tasks"


@dataclass
class ResolvedDecisionContext:
    """Structured fields parsed from a decision markdown file."""

    decision_id: str
    summary: str | None = None
    known_exceptions: list[str] = field(default_factory=list)
    raw_content: str = ""
    source_path: str = ""


@dataclass
class ResolvedTaskContext:
    """Structured fields parsed from a task markdown file."""

    task_id: str
    objective: str | None = None
    acceptance_criteria: list[str] = field(default_factory=list)
    waived_findings: list[str] = field(default_factory=list)
    raw_content: str = ""
    source_path: str = ""


@dataclass
class ResolvedContext:
    """Combined decision + task context resolved from filesystem."""

    decision: ResolvedDecisionContext | None = None
    task: ResolvedTaskContext | None = None

    def as_dict(self) -> dict[str, Any]:
        """Convert to the task_context dict format the orchestrator expects."""
        result: dict[str, Any] = {}
        if self.decision:
            result["decision_id"] = self.decision.decision_id
            if self.decision.summary:
                result["decision_summary"] = self.decision.summary
            if self.decision.known_exceptions:
                result["known_exceptions"] = self.decision.known_exceptions
        if self.task:
            result["task_id"] = self.task.task_id
            if self.task.objective:
                result["task_objective"] = self.task.objective
            if self.task.acceptance_criteria:
                result["acceptance_criteria"] = self.task.acceptance_criteria
            if self.task.waived_findings:
                result["waived_findings"] = self.task.waived_findings
        return result


class ContextResolver:
    """Resolves decision and task context from filesystem markdown files.

    Uses the project connector to read files, so it respects file I/O
    timeouts and health checks already built into the connector layer.
    """

    def __init__(
        self,
        connector: BaseConnector,
        decisions_dir: str = DEFAULT_DECISIONS_DIR,
        tasks_dir: str = DEFAULT_TASKS_DIR,
    ) -> None:
        self.connector = connector
        self.decisions_dir = decisions_dir
        self.tasks_dir = tasks_dir

    async def resolve(
        self,
        decision_id: str | None = None,
        task_id: str | None = None,
    ) -> ResolvedContext:
        """Resolve context from filesystem. Gracefully degrades if files don't exist."""
        ctx = ResolvedContext()

        if decision_id:
            ctx.decision = await self._resolve_decision(decision_id)

        if task_id:
            ctx.task = await self._resolve_task(task_id)

        return ctx

    async def _resolve_decision(self, decision_id: str) -> ResolvedDecisionContext | None:
        """Read and parse a decision markdown file."""
        rel_path = f"{self.decisions_dir}/{decision_id}.md"
        try:
            content = await self.connector.read_file(rel_path)
        except (FileNotFoundError, TimeoutError) as exc:
            logger.info(
                "[CONTEXT-RESOLVER] Decision file not found or timed out: %s (%s)",
                rel_path, exc,
            )
            return None

        logger.info("[CONTEXT-RESOLVER] Resolved decision context from %s", rel_path)
        return ResolvedDecisionContext(
            decision_id=decision_id,
            summary=_extract_section(content, "Summary")
            or _extract_section(content, "Decision Summary")
            or _extract_first_paragraph(content),
            known_exceptions=_extract_list_section(content, "Known Exceptions")
            or _extract_list_section(content, "Exceptions"),
            raw_content=content,
            source_path=rel_path,
        )

    async def _resolve_task(self, task_id: str) -> ResolvedTaskContext | None:
        """Read and parse a task markdown file."""
        rel_path = f"{self.tasks_dir}/{task_id}.md"
        try:
            content = await self.connector.read_file(rel_path)
        except (FileNotFoundError, TimeoutError) as exc:
            logger.info(
                "[CONTEXT-RESOLVER] Task file not found or timed out: %s (%s)",
                rel_path, exc,
            )
            return None

        logger.info("[CONTEXT-RESOLVER] Resolved task context from %s", rel_path)
        return ResolvedTaskContext(
            task_id=task_id,
            objective=_extract_section(content, "Objective")
            or _extract_section(content, "Task Objective")
            or _extract_first_paragraph(content),
            acceptance_criteria=_extract_list_section(content, "Acceptance Criteria")
            or _extract_list_section(content, "Criteria")
            or _extract_list_section(content, "Done When"),
            waived_findings=_extract_list_section(content, "Waived Findings")
            or _extract_list_section(content, "Waivers"),
            raw_content=content,
            source_path=rel_path,
        )


# ── Markdown Section Parsers ─────────────────────────────────────────────────

# Matches ## or ### level headings
_HEADING_RE = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)


def _extract_section(content: str, heading: str) -> str | None:
    """Extract the text body under a ## or ### heading (case-insensitive).

    Returns the text between the target heading and the next same-or-higher
    level heading, stripped. Returns None if heading not found.
    """
    lines = content.split("\n")
    capture = False
    captured: list[str] = []

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            if capture:
                # We hit the next heading — stop
                break
            if m.group(1).strip().lower() == heading.lower():
                capture = True
                continue
        elif capture:
            captured.append(line)

    text = "\n".join(captured).strip()
    return text if text else None


def _extract_list_section(content: str, heading: str) -> list[str]:
    """Extract a bullet/numbered list from under a heading.

    Returns a list of strings (one per list item). Supports:
    - Bullet lists (-, *, +)
    - Numbered lists (1., 2., etc.)
    - Multi-line items (continuation lines indented 2+ spaces)
    """
    section = _extract_section(content, heading)
    if not section:
        return []

    items: list[str] = []
    current_item: list[str] = []

    for line in section.split("\n"):
        stripped = line.strip()
        # New list item
        list_match = re.match(r"^[-*+]\s+(.+)$", stripped) or re.match(
            r"^\d+[.)]\s+(.+)$", stripped
        )
        if list_match:
            if current_item:
                items.append(" ".join(current_item))
            current_item = [list_match.group(1).strip()]
        elif stripped and current_item and (line.startswith("  ") or line.startswith("\t")):
            # Continuation of previous item
            current_item.append(stripped)
        elif not stripped:
            # Blank line — flush current item
            if current_item:
                items.append(" ".join(current_item))
                current_item = []

    if current_item:
        items.append(" ".join(current_item))

    return items


def _extract_first_paragraph(content: str) -> str | None:
    """Extract the first non-heading, non-empty paragraph as a fallback summary.

    Skips YAML frontmatter (--- blocks) and headings.
    """
    lines = content.split("\n")
    in_frontmatter = False
    para_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if stripped.startswith("#"):
            if para_lines:
                break
            continue
        if not stripped:
            if para_lines:
                break
            continue
        para_lines.append(stripped)

    text = " ".join(para_lines).strip()
    return text if text else None
