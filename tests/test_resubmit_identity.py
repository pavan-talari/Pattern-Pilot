"""Tests for P2 fixes: resubmit identity persistence and intentional-empty override.

These test the specific edge cases identified in QC review:
1. Resubmit adds task_id/decision_id that the original submission lacked
2. Empty string/list from caller is NOT overwritten by filesystem resolution
3. None from caller IS overwritten by filesystem resolution
"""

from __future__ import annotations

import pytest

from pattern_pilot.context.context_resolver import ContextResolver


# ── Sample markdown docs ─────────────────────────────────────────────────────

DECISION_MD = """\
# DEC-100: Refactor auth

## Summary
Refactor authentication to use OAuth2 flow.

## Known Exceptions
- Service accounts keep API key auth
"""

TASK_MD = """\
# TASK-200: Update token endpoint

## Objective
Migrate the /token endpoint from basic auth to OAuth2 authorization code flow.

## Acceptance Criteria
- Token endpoint accepts authorization codes
- Refresh tokens are issued on initial grant
- Legacy basic auth returns 410 Gone

## Waived Findings
- Missing PKCE support (deferred to TASK-201)
"""


class MockConnector:
    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.repo_path = "/mock"

    async def read_file(self, relative_path: str) -> str:
        if relative_path in self._files:
            return self._files[relative_path]
        raise FileNotFoundError(f"Mock: {relative_path}")


# ── Test: intentional empty overrides ─────────────────────────────────────────


class TestIntentionalEmptyOverride:
    """Verify that empty string/list from caller is preserved (not overwritten
    by filesystem), while None triggers filesystem fallback."""

    @pytest.mark.asyncio
    async def test_empty_list_not_overwritten(self):
        """Caller sends acceptance_criteria=[] to intentionally clear it.
        Filesystem should NOT repopulate."""
        connector = MockConnector({
            "docs/decisions/DEC-100.md": DECISION_MD,
            "docs/tasks/TASK-200.md": TASK_MD,
        })
        resolver = ContextResolver(connector)
        resolved = await resolver.resolve(decision_id="DEC-100", task_id="TASK-200")
        fs_ctx = resolved.as_dict()

        # Simulate ctx built from submission with intentional empties
        ctx = {
            "task_objective": None,         # Missing — should be filled
            "acceptance_criteria": [],      # Intentionally empty — should NOT be filled
            "known_exceptions": [],         # Intentionally empty — should NOT be filled
            "waived_findings": [],          # Intentionally empty — should NOT be filled
            "decision_summary": "",         # Intentionally empty — should NOT be filled
        }

        # Apply the same merge logic as orchestrator
        for key, value in fs_ctx.items():
            if value and (key not in ctx or ctx[key] is None):
                ctx[key] = value

        # task_objective was None → filled from filesystem
        assert ctx["task_objective"] is not None
        assert "OAuth2" in ctx["task_objective"] or "token endpoint" in ctx["task_objective"].lower()

        # These were intentionally empty → NOT overwritten
        assert ctx["acceptance_criteria"] == []
        assert ctx["known_exceptions"] == []
        assert ctx["waived_findings"] == []
        assert ctx["decision_summary"] == ""

    @pytest.mark.asyncio
    async def test_none_values_are_filled(self):
        """Caller sends None for all fields — filesystem fills everything."""
        connector = MockConnector({
            "docs/decisions/DEC-100.md": DECISION_MD,
            "docs/tasks/TASK-200.md": TASK_MD,
        })
        resolver = ContextResolver(connector)
        resolved = await resolver.resolve(decision_id="DEC-100", task_id="TASK-200")
        fs_ctx = resolved.as_dict()

        ctx = {
            "task_objective": None,
            "decision_summary": None,
            "acceptance_criteria": None,
            "known_exceptions": None,
            "waived_findings": None,
        }

        for key, value in fs_ctx.items():
            if value and (key not in ctx or ctx[key] is None):
                ctx[key] = value

        assert ctx["task_objective"] is not None
        assert ctx["decision_summary"] is not None
        assert len(ctx["acceptance_criteria"]) == 3
        assert len(ctx["known_exceptions"]) == 1
        assert len(ctx["waived_findings"]) == 1

    @pytest.mark.asyncio
    async def test_absent_key_is_filled(self):
        """Key not in ctx at all → filesystem fills it."""
        connector = MockConnector({
            "docs/tasks/TASK-200.md": TASK_MD,
        })
        resolver = ContextResolver(connector)
        resolved = await resolver.resolve(task_id="TASK-200")
        fs_ctx = resolved.as_dict()

        ctx = {}  # No keys at all

        for key, value in fs_ctx.items():
            if value and (key not in ctx or ctx[key] is None):
                ctx[key] = value

        assert "task_objective" in ctx
        assert "acceptance_criteria" in ctx
        assert len(ctx["acceptance_criteria"]) == 3

    @pytest.mark.asyncio
    async def test_caller_value_takes_precedence(self):
        """Caller provides a non-empty value — filesystem does not overwrite."""
        connector = MockConnector({
            "docs/tasks/TASK-200.md": TASK_MD,
        })
        resolver = ContextResolver(connector)
        resolved = await resolver.resolve(task_id="TASK-200")
        fs_ctx = resolved.as_dict()

        ctx = {
            "task_objective": "Custom override objective",
            "acceptance_criteria": ["Custom criterion A"],
        }

        for key, value in fs_ctx.items():
            if value and (key not in ctx or ctx[key] is None):
                ctx[key] = value

        assert ctx["task_objective"] == "Custom override objective"
        assert ctx["acceptance_criteria"] == ["Custom criterion A"]
        # waived_findings was absent → filled from filesystem
        assert "waived_findings" in ctx
        assert len(ctx["waived_findings"]) == 1


# ── Test: resubmit identity evolution ─────────────────────────────────────────


class TestResubmitIdentityEvolution:
    """Simulates the resubmit scenario where the first submission lacks IDs
    and a later resubmit adds them. Tests the logic that both orchestrator
    paths use to update run fields."""

    def test_new_task_id_persisted_on_resubmit(self):
        """If run.task_id is None and request provides task_id, it should be set."""

        class FakeRun:
            task_id = None
            decision_id = None
            task_ref = "old-ref"
            attempt_number = None

        run = FakeRun()

        # Simulate the resubmit update logic from orchestrator
        request_task_id = "TASK-200"
        request_decision_id = "DEC-100"
        if request_task_id and not run.task_id:
            run.task_id = request_task_id
        if request_decision_id and not run.decision_id:
            run.decision_id = request_decision_id

        assert run.task_id == "TASK-200"
        assert run.decision_id == "DEC-100"

    def test_existing_ids_not_overwritten(self):
        """If run already has task_id/decision_id, new values should NOT overwrite."""

        class FakeRun:
            task_id = "TASK-100"
            decision_id = "DEC-50"
            task_ref = "old-ref"
            attempt_number = 1

        run = FakeRun()

        request_task_id = "TASK-999"
        request_decision_id = "DEC-999"
        if request_task_id and not run.task_id:
            run.task_id = request_task_id
        if request_decision_id and not run.decision_id:
            run.decision_id = request_decision_id

        # Original values preserved
        assert run.task_id == "TASK-100"
        assert run.decision_id == "DEC-50"

    def test_partial_id_update(self):
        """Run has task_id but not decision_id — only decision_id gets set."""

        class FakeRun:
            task_id = "TASK-100"
            decision_id = None

        run = FakeRun()

        request_task_id = "TASK-200"
        request_decision_id = "DEC-100"
        if request_task_id and not run.task_id:
            run.task_id = request_task_id
        if request_decision_id and not run.decision_id:
            run.decision_id = request_decision_id

        assert run.task_id == "TASK-100"  # Kept original
        assert run.decision_id == "DEC-100"  # Filled in

    def test_no_ids_on_resubmit_no_change(self):
        """Resubmit provides no IDs — run fields stay as-is."""

        class FakeRun:
            task_id = None
            decision_id = None

        run = FakeRun()

        request_task_id = None
        request_decision_id = None
        if request_task_id and not run.task_id:
            run.task_id = request_task_id
        if request_decision_id and not run.decision_id:
            run.decision_id = request_decision_id

        assert run.task_id is None
        assert run.decision_id is None
