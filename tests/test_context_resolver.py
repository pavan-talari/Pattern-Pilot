"""Tests for Phase 2: filesystem context resolver."""

from __future__ import annotations

import pytest

from pattern_pilot.context.context_resolver import (
    ContextResolver,
    _extract_first_paragraph,
    _extract_list_section,
    _extract_section,
)


# ── Markdown parsing tests ───────────────────────────────────────────────────


SAMPLE_DECISION_MD = """\
---
id: DEC-302
title: Auth middleware rewrite
status: approved
---

# DEC-302: Auth middleware rewrite

## Summary

Rewrite the authentication middleware to use JWT tokens instead of
session-based auth. This is driven by legal/compliance requirements
around session token storage.

## Known Exceptions

- Legacy endpoints (/api/v1/*) keep old session auth until Q3 deprecation
- Internal health-check endpoints bypass auth entirely
- Service-to-service calls use mTLS, not JWT

## Scope

All user-facing API endpoints in the main gateway.

## Notes

See compliance ticket LEGAL-445 for full requirements.
"""

SAMPLE_TASK_MD = """\
---
id: TASK-653
decision: DEC-302
status: in_progress
---

# TASK-653: Implement JWT validation middleware

## Objective

Replace the session-based auth middleware with a JWT validation layer
that checks token signatures, expiry, and required claims before
allowing requests through to handlers.

## Acceptance Criteria

- All protected endpoints reject requests without valid JWT
- Token expiry is enforced with 5-minute clock skew tolerance
- Required claims (sub, aud, iss) are validated
- Invalid tokens return 401 with descriptive error body

## Waived Findings

- Missing docstring on internal token-parsing helper (intentional — private API)
- Cyclomatic complexity on validate_claims (acceptable for security code)

## Implementation Notes

Start with the gateway middleware, then add per-route overrides.
"""

MINIMAL_TASK_MD = """\
# TASK-100: Quick fix

Fix the broken import in utils.py that causes startup crash.
"""


class TestExtractSection:
    def test_extracts_summary(self):
        result = _extract_section(SAMPLE_DECISION_MD, "Summary")
        assert result is not None
        assert "JWT tokens" in result
        assert "compliance" in result

    def test_extracts_objective(self):
        result = _extract_section(SAMPLE_TASK_MD, "Objective")
        assert result is not None
        assert "JWT validation layer" in result

    def test_case_insensitive(self):
        result = _extract_section(SAMPLE_DECISION_MD, "summary")
        assert result is not None
        assert "JWT tokens" in result

    def test_returns_none_for_missing_heading(self):
        result = _extract_section(SAMPLE_DECISION_MD, "Nonexistent Section")
        assert result is None

    def test_stops_at_next_heading(self):
        result = _extract_section(SAMPLE_DECISION_MD, "Summary")
        assert "Legacy endpoints" not in result  # That's in Known Exceptions

    def test_handles_notes_at_end(self):
        result = _extract_section(SAMPLE_DECISION_MD, "Notes")
        assert result is not None
        assert "LEGAL-445" in result


class TestExtractListSection:
    def test_extracts_known_exceptions(self):
        items = _extract_list_section(SAMPLE_DECISION_MD, "Known Exceptions")
        assert len(items) == 3
        assert "Legacy endpoints" in items[0]
        assert "health-check" in items[1]
        assert "mTLS" in items[2]

    def test_extracts_acceptance_criteria(self):
        items = _extract_list_section(SAMPLE_TASK_MD, "Acceptance Criteria")
        assert len(items) == 4
        assert "reject requests" in items[0]
        assert "clock skew" in items[1]

    def test_extracts_waived_findings(self):
        items = _extract_list_section(SAMPLE_TASK_MD, "Waived Findings")
        assert len(items) == 2
        assert "docstring" in items[0]
        assert "Cyclomatic" in items[1]

    def test_returns_empty_for_missing_section(self):
        items = _extract_list_section(SAMPLE_DECISION_MD, "Nonexistent")
        assert items == []

    def test_returns_empty_for_non_list_section(self):
        # Scope section has no list items
        items = _extract_list_section(SAMPLE_DECISION_MD, "Scope")
        assert items == []


class TestExtractFirstParagraph:
    def test_skips_frontmatter_and_heading(self):
        result = _extract_first_paragraph(SAMPLE_DECISION_MD)
        assert result is not None
        assert "Rewrite the authentication" in result

    def test_minimal_doc(self):
        result = _extract_first_paragraph(MINIMAL_TASK_MD)
        assert result is not None
        assert "broken import" in result

    def test_empty_content(self):
        assert _extract_first_paragraph("") is None
        assert _extract_first_paragraph("---\nid: x\n---") is None


# ── Resolver integration tests (mock connector) ─────────────────────────────


class MockConnector:
    """Minimal connector mock that serves files from a dict."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.repo_path = "/mock"

    async def read_file(self, relative_path: str) -> str:
        if relative_path in self._files:
            return self._files[relative_path]
        raise FileNotFoundError(f"Mock: {relative_path}")


class TestContextResolver:
    @pytest.mark.asyncio
    async def test_resolves_decision_context(self):
        connector = MockConnector({
            "docs/decisions/DEC-302.md": SAMPLE_DECISION_MD,
        })
        resolver = ContextResolver(connector)
        result = await resolver.resolve(decision_id="DEC-302")

        assert result.decision is not None
        assert result.decision.decision_id == "DEC-302"
        assert "JWT tokens" in (result.decision.summary or "")
        assert len(result.decision.known_exceptions) == 3
        assert result.task is None

    @pytest.mark.asyncio
    async def test_resolves_task_context(self):
        connector = MockConnector({
            "docs/tasks/TASK-653.md": SAMPLE_TASK_MD,
        })
        resolver = ContextResolver(connector)
        result = await resolver.resolve(task_id="TASK-653")

        assert result.task is not None
        assert result.task.task_id == "TASK-653"
        assert "JWT validation" in (result.task.objective or "")
        assert len(result.task.acceptance_criteria) == 4
        assert len(result.task.waived_findings) == 2
        assert result.decision is None

    @pytest.mark.asyncio
    async def test_resolves_both(self):
        connector = MockConnector({
            "docs/decisions/DEC-302.md": SAMPLE_DECISION_MD,
            "docs/tasks/TASK-653.md": SAMPLE_TASK_MD,
        })
        resolver = ContextResolver(connector)
        result = await resolver.resolve(decision_id="DEC-302", task_id="TASK-653")

        assert result.decision is not None
        assert result.task is not None

    @pytest.mark.asyncio
    async def test_graceful_on_missing_files(self):
        connector = MockConnector({})  # No files
        resolver = ContextResolver(connector)
        result = await resolver.resolve(decision_id="DEC-999", task_id="TASK-999")

        assert result.decision is None
        assert result.task is None

    @pytest.mark.asyncio
    async def test_custom_directories(self):
        connector = MockConnector({
            "custom/dec/DEC-1.md": SAMPLE_DECISION_MD,
            "custom/tsk/TASK-1.md": SAMPLE_TASK_MD,
        })
        resolver = ContextResolver(
            connector,
            decisions_dir="custom/dec",
            tasks_dir="custom/tsk",
        )
        result = await resolver.resolve(decision_id="DEC-1", task_id="TASK-1")

        assert result.decision is not None
        assert result.task is not None

    @pytest.mark.asyncio
    async def test_as_dict_output(self):
        connector = MockConnector({
            "docs/decisions/DEC-302.md": SAMPLE_DECISION_MD,
            "docs/tasks/TASK-653.md": SAMPLE_TASK_MD,
        })
        resolver = ContextResolver(connector)
        result = await resolver.resolve(decision_id="DEC-302", task_id="TASK-653")
        d = result.as_dict()

        assert d["decision_id"] == "DEC-302"
        assert d["task_id"] == "TASK-653"
        assert "decision_summary" in d
        assert "task_objective" in d
        assert len(d["known_exceptions"]) == 3
        assert len(d["acceptance_criteria"]) == 4
        assert len(d["waived_findings"]) == 2

    @pytest.mark.asyncio
    async def test_minimal_doc_fallback_to_first_paragraph(self):
        connector = MockConnector({
            "docs/tasks/TASK-100.md": MINIMAL_TASK_MD,
        })
        resolver = ContextResolver(connector)
        result = await resolver.resolve(task_id="TASK-100")

        assert result.task is not None
        assert "broken import" in (result.task.objective or "")
        assert result.task.acceptance_criteria == []

    @pytest.mark.asyncio
    async def test_no_ids_returns_empty(self):
        connector = MockConnector({})
        resolver = ContextResolver(connector)
        result = await resolver.resolve()

        assert result.decision is None
        assert result.task is None
        assert result.as_dict() == {}
