"""Tests for Phase 1: stable identity, iteration policy, context fields."""

from __future__ import annotations


from pattern_pilot.core.contracts import (
    ContextBundle,
    Finding,
    FindingSeverity,
    FindingTier,
    ReviewProfile,
    ReviewRoundResult,
    SubmitRequest,
    Verdict,
)
from pattern_pilot.core.orchestrator import Orchestrator


# ── Stable identity tests ─────────────────────────────────────────────────


class TestStableIdentity:
    """SubmitRequest carries stable task_id, decision_id, attempt_number."""

    def test_task_id_defaults_to_none(self):
        req = SubmitRequest(
            project_name="test", task_ref="TASK-1", files_changed=["a.py"]
        )
        assert req.task_id is None
        assert req.decision_id is None
        assert req.attempt_number is None

    def test_task_id_explicitly_set(self):
        req = SubmitRequest(
            project_name="test",
            task_ref="Source-aware prompt families",
            task_id="TASK-653",
            decision_id="DEC-302",
            attempt_number=3,
            files_changed=["a.py"],
        )
        assert req.task_id == "TASK-653"
        assert req.decision_id == "DEC-302"
        assert req.attempt_number == 3
        # task_ref is display-only, not used for lookup
        assert req.task_ref == "Source-aware prompt families"

    def test_context_bundle_carries_identity(self):
        bundle = ContextBundle(
            project_name="test",
            task_ref="TASK-1 round 2",
            review_profile=ReviewProfile.STANDARD,
            task_id="TASK-1",
            decision_id="DEC-100",
            attempt_number=2,
        )
        assert bundle.task_id == "TASK-1"
        assert bundle.decision_id == "DEC-100"
        assert bundle.attempt_number == 2

    def test_task_context_fields_on_request(self):
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            files_changed=["a.py"],
            task_objective="Fix the auth bug",
            acceptance_criteria=["Login works", "Tokens refresh"],
            known_exceptions=["Legacy endpoints keep old auth"],
            waived_findings=["Missing docstring on internal helper"],
            decision_summary="Auth refactor for compliance",
        )
        assert req.task_objective == "Fix the auth bug"
        assert len(req.acceptance_criteria) == 2
        assert len(req.known_exceptions) == 1
        assert len(req.waived_findings) == 1
        assert req.decision_summary == "Auth refactor for compliance"

    def test_context_bundle_carries_task_context(self):
        bundle = ContextBundle(
            project_name="test",
            task_ref="T-1",
            review_profile=ReviewProfile.STANDARD,
            task_objective="Fix auth",
            acceptance_criteria=["Login works"],
            known_exceptions=["Legacy OK"],
            waived_findings=["Missing docstring"],
            decision_summary="Auth refactor",
        )
        assert bundle.task_objective == "Fix auth"
        assert bundle.acceptance_criteria == ["Login works"]
        assert bundle.known_exceptions == ["Legacy OK"]
        assert bundle.waived_findings == ["Missing docstring"]
        assert bundle.decision_summary == "Auth refactor"


# ── Iteration policy tests ────────────────────────────────────────────────


class TestIterationPolicy:
    """Orchestrator._apply_iteration_policy downgrades repeated weak findings."""

    def _make_finding(
        self,
        tier: FindingTier = FindingTier.BLOCKING,
        severity: FindingSeverity = FindingSeverity.MEDIUM,
        file_path: str = "app/service.py",
        message: str = "Some issue",
        confidence: float = 0.7,
    ) -> Finding:
        return Finding(
            tier=tier,
            category="correctness",
            file_path=file_path,
            message=message,
            severity=severity,
            confidence=confidence,
        )

    def test_no_policy_on_round_1(self):
        """Round 1 should never downgrade anything (no prior findings)."""
        finding = self._make_finding(severity=FindingSeverity.LOW)
        result = ReviewRoundResult(
            round_number=1,
            verdict=Verdict.BLOCKING,
            findings=[finding],
        )
        # Round 1 has no prior findings — policy is only called when prior_findings exist
        out = Orchestrator._apply_iteration_policy(result, [], 1)
        assert out.findings[0].tier == FindingTier.BLOCKING

    def test_no_policy_on_round_2(self):
        """Round 2 should not trigger the policy even with prior findings."""
        finding = self._make_finding(severity=FindingSeverity.LOW)
        prior = self._make_finding(severity=FindingSeverity.LOW)
        result = ReviewRoundResult(
            round_number=2,
            verdict=Verdict.BLOCKING,
            findings=[finding],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 2)
        # Round < 3, so no downgrade
        assert out.findings[0].tier == FindingTier.BLOCKING

    def test_medium_downgraded_on_round_3(self):
        """Round 3: repeated medium-severity finding should be downgraded."""
        prior = self._make_finding(
            message="Null check missing in process_order",
            severity=FindingSeverity.MEDIUM,
        )
        current = self._make_finding(
            message="Null check missing in process_order",
            severity=FindingSeverity.MEDIUM,
        )
        result = ReviewRoundResult(
            round_number=3,
            verdict=Verdict.BLOCKING,
            findings=[current],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 3)
        assert out.findings[0].tier == FindingTier.RECOMMENDED_REVIEW
        assert out.verdict == Verdict.PASS_WITH_ADVISORIES

    def test_low_downgraded_on_round_3(self):
        """Round 3: repeated low-severity finding should be downgraded."""
        prior = self._make_finding(
            message="Function too long, consider splitting",
            severity=FindingSeverity.LOW,
        )
        current = self._make_finding(
            message="Function too long, consider splitting",
            severity=FindingSeverity.LOW,
        )
        result = ReviewRoundResult(
            round_number=3,
            verdict=Verdict.BLOCKING,
            findings=[current],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 3)
        assert out.findings[0].tier == FindingTier.RECOMMENDED_REVIEW
        assert out.verdict == Verdict.PASS_WITH_ADVISORIES

    def test_high_severity_never_downgraded(self):
        """High-severity findings should NEVER be downgraded, even on round 5."""
        prior = self._make_finding(
            message="SQL injection in user input handler",
            severity=FindingSeverity.HIGH,
            confidence=0.95,
        )
        current = self._make_finding(
            message="SQL injection in user input handler",
            severity=FindingSeverity.HIGH,
            confidence=0.95,
        )
        result = ReviewRoundResult(
            round_number=5,
            verdict=Verdict.BLOCKING,
            findings=[current],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 5)
        assert out.findings[0].tier == FindingTier.BLOCKING
        assert out.verdict == Verdict.BLOCKING

    def test_mixed_findings_partial_downgrade(self):
        """Mix of high + medium: medium downgraded, high stays, still blocking."""
        prior = [
            self._make_finding(
                message="Missing null check",
                severity=FindingSeverity.MEDIUM,
                file_path="app/service.py",
            ),
            self._make_finding(
                message="Auth bypass vulnerability",
                severity=FindingSeverity.HIGH,
                file_path="app/auth.py",
            ),
        ]
        current_findings = [
            self._make_finding(
                message="Missing null check",
                severity=FindingSeverity.MEDIUM,
                file_path="app/service.py",
            ),
            self._make_finding(
                message="Auth bypass vulnerability",
                severity=FindingSeverity.HIGH,
                file_path="app/auth.py",
            ),
        ]
        result = ReviewRoundResult(
            round_number=3,
            verdict=Verdict.BLOCKING,
            findings=current_findings,
        )
        out = Orchestrator._apply_iteration_policy(result, prior, 3)
        # Medium downgraded, high stays
        assert out.findings[0].tier == FindingTier.RECOMMENDED_REVIEW
        assert out.findings[1].tier == FindingTier.BLOCKING
        # Still blocking because one high-severity finding remains
        assert out.verdict == Verdict.BLOCKING

    def test_new_finding_not_downgraded(self):
        """A genuinely new finding on round 3 should NOT be downgraded."""
        prior = self._make_finding(
            message="Old issue in helper function",
            severity=FindingSeverity.MEDIUM,
            file_path="app/old.py",
        )
        new = self._make_finding(
            message="New bug introduced by fix",
            severity=FindingSeverity.MEDIUM,
            file_path="app/new.py",
        )
        result = ReviewRoundResult(
            round_number=3,
            verdict=Verdict.BLOCKING,
            findings=[new],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 3)
        # New finding doesn't match prior → stays blocking
        assert out.findings[0].tier == FindingTier.BLOCKING
        assert out.verdict == Verdict.BLOCKING

    def test_recommended_autofix_also_downgraded(self):
        """recommended_autofix findings should also be eligible for downgrade."""
        prior = self._make_finding(
            tier=FindingTier.RECOMMENDED_AUTOFIX,
            message="Import unused module",
            severity=FindingSeverity.LOW,
        )
        current = self._make_finding(
            tier=FindingTier.RECOMMENDED_AUTOFIX,
            message="Import unused module",
            severity=FindingSeverity.LOW,
        )
        result = ReviewRoundResult(
            round_number=3,
            verdict=Verdict.BLOCKING,
            findings=[current],
        )
        out = Orchestrator._apply_iteration_policy(result, [prior], 3)
        assert out.findings[0].tier == FindingTier.RECOMMENDED_REVIEW
        assert out.verdict == Verdict.PASS_WITH_ADVISORIES
