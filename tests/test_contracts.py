"""Tests for core contracts — enums, Pydantic validation."""

from pattern_pilot.core.contracts import (
    Finding,
    FindingStatus,
    FindingTier,
    ReviewProfile,
    ReviewStatus,
    SubmitRequest,
    SubmitResponse,
    Verdict,
)


class TestEnums:
    def test_finding_tier_values(self):
        assert FindingTier.BLOCKING.value == "blocking"
        assert FindingTier.RECOMMENDED_AUTOFIX.value == "recommended_autofix"
        assert FindingTier.RECOMMENDED_REVIEW.value == "recommended_review"
        assert FindingTier.ADVISORY.value == "advisory"

    def test_verdict_values(self):
        assert Verdict.BLOCKING.value == "blocking"
        assert Verdict.REQUIRES_HUMAN_REVIEW.value == "requires_human_review"
        assert Verdict.PASS_WITH_ADVISORIES.value == "pass_with_advisories"
        assert Verdict.PASS.value == "pass"

    def test_review_status_values(self):
        assert ReviewStatus.PENDING.value == "pending"
        assert ReviewStatus.RUNNING.value == "running"
        assert ReviewStatus.PASSED.value == "passed"
        assert ReviewStatus.ESCALATED.value == "escalated"

    def test_review_profile_values(self):
        assert ReviewProfile.QUICK.value == "quick"
        assert ReviewProfile.STANDARD.value == "standard"
        assert ReviewProfile.DEEP.value == "deep"


class TestFindingModel:
    def test_minimal_finding(self):
        f = Finding(
            tier=FindingTier.BLOCKING,
            category="security",
            file_path="main.py",
            message="Issue found",
        )
        assert f.tier == FindingTier.BLOCKING
        assert f.autofix_safe is False
        assert f.status == FindingStatus.OPEN
        assert f.line_start is None

    def test_full_finding(self):
        f = Finding(
            tier=FindingTier.RECOMMENDED_AUTOFIX,
            category="style",
            file_path="utils.py",
            line_start=10,
            line_end=15,
            message="Missing type hints",
            suggestion="Add annotations",
            autofix_safe=True,
        )
        assert f.line_start == 10
        assert f.autofix_safe is True


class TestSubmitRequest:
    def test_defaults(self):
        req = SubmitRequest(
            project_name="my-project",
            task_ref="TASK-1",
        )
        assert req.review_profile == ReviewProfile.STANDARD
        assert req.force_review is False
        assert req.files_changed == []
        assert req.use_git_diff is False
        assert req.diff_base == "HEAD"
        assert req.diff_scope == "unstaged"

    def test_with_profile(self):
        req = SubmitRequest(
            project_name="my-project",
            task_ref="TASK-1",
            review_profile=ReviewProfile.DEEP,
            files_changed=["a.py", "b.py"],
        )
        assert req.review_profile == ReviewProfile.DEEP
        assert len(req.files_changed) == 2


class TestSubmitResponse:
    def test_serialization(self):
        resp = SubmitResponse(
            run_id="abc-123",
            status=ReviewStatus.RUNNING,
            verdict=Verdict.BLOCKING,
            round_number=1,
            message="Fix issues",
            requires_resubmit=True,
        )
        data = resp.model_dump()
        assert data["run_id"] == "abc-123"
        assert data["requires_resubmit"] is True
