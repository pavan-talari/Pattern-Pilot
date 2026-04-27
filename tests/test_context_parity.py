"""Tests for Phase 2 parity: filesystem resolution and iteration policy
must behave identically across the MCP (execute_round) and synchronous
(submit_for_review) code paths.

Also tests partial payload override behavior — submission payload fields
take precedence, filesystem fills in blanks only.
"""

from __future__ import annotations

import pytest

from pattern_pilot.context.context_resolver import ContextResolver


# ── Sample markdown docs ─────────────────────────────────────────────────────

DECISION_MD = """\
---
id: DEC-500
---
# DEC-500: Migrate to event sourcing

## Summary
Migrate order processing from CRUD to event-sourced architecture.

## Known Exceptions
- Legacy batch jobs keep direct DB writes until v3
- Admin dashboard reads from read-model only
"""

TASK_MD = """\
---
id: TASK-800
decision: DEC-500
---
# TASK-800: Implement OrderPlaced event handler

## Objective
Create the event handler that processes OrderPlaced events and updates
the read-model projections.

## Acceptance Criteria
- OrderPlaced events are consumed within 5s
- Read-model is updated atomically
- Failed projections retry with exponential backoff

## Waived Findings
- Missing retry test for backoff timing (flaky in CI, covered by integration suite)
"""


class MockConnector:
    """Connector mock serving markdown files from a dict."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.repo_path = "/mock"

    async def read_file(self, relative_path: str) -> str:
        if relative_path in self._files:
            return self._files[relative_path]
        raise FileNotFoundError(f"Mock: {relative_path}")


# ── Test: partial payload override does NOT skip filesystem resolution ────────


class TestPartialPayloadOverride:
    """P2 fix: supplying one field (e.g., task_objective) should not prevent
    filesystem resolution of other missing fields (e.g., known_exceptions).
    """

    @pytest.mark.asyncio
    async def test_partial_objective_still_resolves_decision(self):
        """If caller provides task_objective but not decision_summary,
        filesystem should fill in the decision fields."""
        connector = MockConnector({
            "docs/decisions/DEC-500.md": DECISION_MD,
            "docs/tasks/TASK-800.md": TASK_MD,
        })
        resolver = ContextResolver(connector)

        # Simulate: caller provided task_objective manually
        resolved = await resolver.resolve(decision_id="DEC-500", task_id="TASK-800")
        fs_ctx = resolved.as_dict()

        # Build a submission context where only task_objective is pre-filled
        submit_ctx = {
            "task_objective": "Custom caller-provided objective",
            "decision_summary": None,
            "known_exceptions": [],
            "acceptance_criteria": [],
            "waived_findings": [],
        }

        # Merge: filesystem fills blanks, caller values take precedence
        for key, value in fs_ctx.items():
            if value and not submit_ctx.get(key):
                submit_ctx[key] = value

        # Caller's objective is preserved
        assert submit_ctx["task_objective"] == "Custom caller-provided objective"
        # Filesystem-resolved fields are filled in
        assert submit_ctx["decision_summary"] is not None
        assert "event sourcing" in submit_ctx["decision_summary"].lower() or "event-sourced" in submit_ctx["decision_summary"].lower()
        assert len(submit_ctx["known_exceptions"]) == 2
        assert len(submit_ctx["acceptance_criteria"]) == 3
        assert len(submit_ctx["waived_findings"]) == 1

    @pytest.mark.asyncio
    async def test_full_payload_overrides_filesystem(self):
        """If caller provides ALL fields, filesystem-resolved values are ignored."""
        connector = MockConnector({
            "docs/decisions/DEC-500.md": DECISION_MD,
            "docs/tasks/TASK-800.md": TASK_MD,
        })
        resolver = ContextResolver(connector)
        resolved = await resolver.resolve(decision_id="DEC-500", task_id="TASK-800")
        fs_ctx = resolved.as_dict()

        submit_ctx = {
            "task_objective": "Custom objective",
            "decision_summary": "Custom summary",
            "known_exceptions": ["Custom exception"],
            "acceptance_criteria": ["Custom criterion"],
            "waived_findings": ["Custom waiver"],
        }

        # Merge: nothing overwritten because all fields are populated
        for key, value in fs_ctx.items():
            if value and not submit_ctx.get(key):
                submit_ctx[key] = value

        assert submit_ctx["task_objective"] == "Custom objective"
        assert submit_ctx["decision_summary"] == "Custom summary"
        assert submit_ctx["known_exceptions"] == ["Custom exception"]
        assert submit_ctx["acceptance_criteria"] == ["Custom criterion"]
        assert submit_ctx["waived_findings"] == ["Custom waiver"]

    @pytest.mark.asyncio
    async def test_empty_filesystem_no_crash(self):
        """No markdown docs at all — context stays as submitted."""
        connector = MockConnector({})
        resolver = ContextResolver(connector)
        resolved = await resolver.resolve(decision_id="DEC-999", task_id="TASK-999")
        fs_ctx = resolved.as_dict()

        submit_ctx = {"task_objective": "My objective"}
        for key, value in fs_ctx.items():
            if value and not submit_ctx.get(key):
                submit_ctx[key] = value

        assert submit_ctx == {"task_objective": "My objective"}


# ── Test: iteration policy parity ────────────────────────────────────────────

from pattern_pilot.core.contracts import (
    Finding,
    FindingSeverity,
    FindingTier,
    ReviewRoundResult,
    Verdict,
)
from pattern_pilot.core.orchestrator import Orchestrator


class TestIterationPolicyParity:
    """Both submit_for_review and execute_round use _apply_iteration_policy.
    These tests verify the shared static method behaves correctly for the
    scenarios both paths encounter.
    """

    @staticmethod
    def _make_finding(
        severity: FindingSeverity = FindingSeverity.MEDIUM,
        tier: FindingTier = FindingTier.BLOCKING,
        message: str = "Repeated issue in foo.py",
        file_path: str = "foo.py",
    ) -> Finding:
        return Finding(
            tier=tier,
            category="correctness",
            file_path=file_path,
            message=message,
            severity=severity,
        )

    def test_round_3_with_matching_priors_downgrades(self):
        """On round 3, matching medium-severity prior findings get downgraded."""
        finding = self._make_finding(severity=FindingSeverity.MEDIUM)
        prior = self._make_finding(severity=FindingSeverity.MEDIUM)
        result = ReviewRoundResult(
            round_number=3,
            verdict=Verdict.BLOCKING,
            findings=[finding],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 3)
        assert out.findings[0].tier == FindingTier.RECOMMENDED_REVIEW
        assert out.verdict == Verdict.PASS_WITH_ADVISORIES

    def test_round_3_high_severity_never_downgraded(self):
        """High severity findings stay blocking even on round 3+."""
        finding = self._make_finding(severity=FindingSeverity.HIGH)
        prior = self._make_finding(severity=FindingSeverity.HIGH)
        result = ReviewRoundResult(
            round_number=4,
            verdict=Verdict.BLOCKING,
            findings=[finding],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 4)
        assert out.findings[0].tier == FindingTier.BLOCKING
        assert out.verdict == Verdict.BLOCKING

    def test_round_2_never_applies_policy(self):
        """Round 2 should never trigger the iteration policy."""
        finding = self._make_finding(severity=FindingSeverity.LOW)
        prior = self._make_finding(severity=FindingSeverity.LOW)
        result = ReviewRoundResult(
            round_number=2,
            verdict=Verdict.BLOCKING,
            findings=[finding],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 2)
        assert out.findings[0].tier == FindingTier.BLOCKING

    def test_new_finding_on_round_3_not_downgraded(self):
        """New findings (no prior match) stay at original tier even on round 3."""
        finding = self._make_finding(message="Brand new issue")
        prior = self._make_finding(message="Different prior issue")
        result = ReviewRoundResult(
            round_number=3,
            verdict=Verdict.BLOCKING,
            findings=[finding],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 3)
        assert out.findings[0].tier == FindingTier.BLOCKING


# ── Test: reviewer user message template ─────────────────────────────────────

from pattern_pilot.core.contracts import ContextBundle, ReviewProfile
from pattern_pilot.core.reviewer import Reviewer


class TestReviewerMessageHierarchy:
    """The user message template should present context in the
    project → decision → task hierarchy order."""

    def test_decision_context_before_task_context(self):
        """Decision Context section appears before Task Context section."""
        reviewer = Reviewer.__new__(Reviewer)
        bundle = ContextBundle(
            project_name="test-project",
            task_ref="TASK-1",
            review_profile=ReviewProfile.STANDARD,
            decision_id="DEC-1",
            decision_summary="Migrate to event sourcing",
            task_id="TASK-1",
            task_objective="Implement event handler",
            acceptance_criteria=["Events consumed within 5s"],
            known_exceptions=["Legacy batch writes allowed"],
        )
        msg = reviewer._build_user_message(bundle)

        decision_pos = msg.index("## Decision Context")
        task_pos = msg.index("## Task Context")
        assert decision_pos < task_pos, "Decision context should appear before task context"

    def test_project_context_before_decision(self):
        """Project Context section appears before Decision Context."""
        reviewer = Reviewer.__new__(Reviewer)
        bundle = ContextBundle(
            project_name="test-project",
            task_ref="TASK-1",
            review_profile=ReviewProfile.STANDARD,
            decision_id="DEC-1",
            decision_summary="Some decision",
        )
        msg = reviewer._build_user_message(bundle)

        project_pos = msg.index("## Project Context")
        decision_pos = msg.index("## Decision Context")
        assert project_pos < decision_pos

    def test_known_exceptions_in_decision_section(self):
        """Known exceptions appear in the Decision Context section."""
        reviewer = Reviewer.__new__(Reviewer)
        bundle = ContextBundle(
            project_name="test-project",
            task_ref="TASK-1",
            review_profile=ReviewProfile.STANDARD,
            decision_id="DEC-1",
            known_exceptions=["Legacy batch writes allowed"],
        )
        msg = reviewer._build_user_message(bundle)

        assert "Legacy batch writes allowed" in msg
        # Should be within decision section
        decision_start = msg.index("## Decision Context")
        exception_pos = msg.index("Legacy batch writes allowed")
        assert exception_pos > decision_start

    def test_waived_findings_in_task_section(self):
        """Waived findings appear in the Task Context section."""
        reviewer = Reviewer.__new__(Reviewer)
        bundle = ContextBundle(
            project_name="test-project",
            task_ref="TASK-1",
            review_profile=ReviewProfile.STANDARD,
            task_id="TASK-1",
            waived_findings=["Missing retry test"],
        )
        msg = reviewer._build_user_message(bundle)

        assert "Missing retry test" in msg
        task_start = msg.index("## Task Context")
        waiver_pos = msg.index("Missing retry test")
        assert waiver_pos > task_start

    def test_no_context_sections_when_empty(self):
        """No Decision/Task Context sections if no relevant data."""
        reviewer = Reviewer.__new__(Reviewer)
        bundle = ContextBundle(
            project_name="test-project",
            task_ref="TASK-1",
            review_profile=ReviewProfile.STANDARD,
        )
        msg = reviewer._build_user_message(bundle)

        assert "## Decision Context" not in msg
        assert "## Task Context" not in msg
