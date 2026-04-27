"""Core domain contracts — enums, Pydantic models, and value objects."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────────


class FindingTier(str, enum.Enum):
    """Severity tier for a review finding."""

    BLOCKING = "blocking"
    RECOMMENDED_AUTOFIX = "recommended_autofix"
    RECOMMENDED_REVIEW = "recommended_review"
    ADVISORY = "advisory"


class FindingSeverity(str, enum.Enum):
    """Impact severity of a finding."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingCategory(str, enum.Enum):
    """Standardized finding categories."""

    CORRECTNESS = "correctness"
    SECURITY = "security"
    GOVERNANCE = "governance"
    ARCHITECTURE = "architecture"
    TESTING = "testing"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    DOCS = "docs"


class Verdict(str, enum.Enum):
    """Overall verdict for a review run or round."""

    BLOCKING = "blocking"
    REQUIRES_HUMAN_REVIEW = "requires_human_review"
    PASS_WITH_ADVISORIES = "pass_with_advisories"
    PASS = "pass"


class ReviewStatus(str, enum.Enum):
    """Lifecycle status of a review run."""

    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"  # Blocking findings returned, awaiting fix + resubmit
    PASSED = "passed"
    PASSED_WITH_ADVISORIES = "passed_with_advisories"
    ESCALATED = "escalated"
    FAILED = "failed"
    REVIEWER_ERROR = "reviewer_error"


class ReviewProfile(str, enum.Enum):
    """How much context to include in the review."""

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class HumanOverride(str, enum.Enum):
    """What a human decided about a finding."""

    ACCEPTED = "accepted"
    WAIVED = "waived"
    DEFERRED = "deferred"
    FALSE_POSITIVE = "false_positive"


class FindingStatus(str, enum.Enum):
    """Resolution status of an individual finding."""

    OPEN = "open"
    FIXED = "fixed"
    WAIVED = "waived"
    DEFERRED = "deferred"
    FALSE_POSITIVE = "false_positive"


class AdvisoryStatus(str, enum.Enum):
    """Status of a Tier-3 advisory."""

    ACTIVE = "active"
    DISMISSED = "dismissed"
    DEFERRED = "deferred"
    ACKNOWLEDGED = "acknowledged"


class ConnectorCapability(str, enum.Enum):
    """Capabilities a project connector can declare."""

    GOVERNANCE_READ = "governance_read"
    GIT_CONTEXT_READ = "git_context_read"
    TASK_READ = "task_read"
    DEPENDENCY_READ = "dependency_read"
    TEST_READ = "test_read"
    CONTEXT_READ = "context_read"  # Can resolve decision/task docs from filesystem


# ── Pydantic Models ─────────────────────────────────────────────────────────


class Finding(BaseModel):
    """A single review finding from the LLM reviewer."""

    tier: FindingTier
    category: str  # Free text, but guided by FindingCategory enum values
    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    message: str
    suggestion: str | None = None
    autofix_safe: bool = False
    severity: FindingSeverity = FindingSeverity.MEDIUM
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    rule_refs: list[str] = Field(default_factory=list)
    why_now: str | None = None
    autofix_diff: str | None = None  # Unified diff patch for recommended_autofix findings
    status: FindingStatus = FindingStatus.OPEN


class DeterministicResult(BaseModel):
    """Result of a single deterministic check (lint, typecheck, test)."""

    check_name: str
    passed: bool
    output: str = ""
    duration_ms: int = 0


class ReviewRoundResult(BaseModel):
    """Result of one LLM review round."""

    round_number: int
    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)
    model_used: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    duration_ms: int = 0


class ReviewRunResult(BaseModel):
    """Final result of a complete review run (may span multiple rounds)."""

    run_id: str
    project_name: str
    task_ref: str
    status: ReviewStatus
    verdict: Verdict | None = None
    review_profile: ReviewProfile
    total_rounds: int = 0
    total_submissions: int = 0
    rounds: list[ReviewRoundResult] = Field(default_factory=list)
    unresolved_findings: list[Finding] = Field(default_factory=list)
    advisories: list[Finding] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SubmitRequest(BaseModel):
    """Request payload for submit_for_review."""

    project_name: str
    task_ref: str
    files_changed: list[str] = Field(default_factory=list)
    review_profile: ReviewProfile = ReviewProfile.STANDARD
    force_review: bool = False
    use_git_diff: bool = False
    diff_base: str = "HEAD"
    diff_scope: str = "unstaged"

    # Stable identity fields (Phase 1 context-based workflow)
    task_id: str | None = None  # Stable task identity (e.g., "TASK-653"). Used for run lookup.
    decision_id: str | None = None  # Groups tasks under a shared change stream (e.g., "DEC-302")
    attempt_number: int | None = None  # Metadata only — never creates a new run

    # Extended context fields (Phase 2)
    # None = omitted (filesystem fallback applies)
    # Empty string/list = intentionally cleared (preserved, no fallback)
    decision_summary: str | None = None
    task_objective: str | None = None
    acceptance_criteria: list[str] | None = None
    known_exceptions: list[str] | None = None
    waived_findings: list[str] | None = None


class SubmitResponse(BaseModel):
    """Response payload from submit_for_review."""

    run_id: str
    status: ReviewStatus
    verdict: Verdict | None = None
    round_number: int = 0
    findings: list[Finding] = Field(default_factory=list)
    message: str = ""
    requires_resubmit: bool = False


class GovernanceSnapshot(BaseModel):
    """Versioned snapshot of a project's governance files."""

    files: dict[str, str] = Field(default_factory=dict)  # path → content hash
    captured_at: datetime = Field(default_factory=datetime.now)


class ContextBundle(BaseModel):
    """Diff-scoped context package sent to the LLM reviewer."""

    project_name: str
    task_ref: str
    review_profile: ReviewProfile
    run_id: str = ""
    round_number: int = 1

    # Stable identity (Phase 1)
    task_id: str | None = None
    decision_id: str | None = None
    attempt_number: int | None = None

    # Decision + task context (Phase 2)
    decision_summary: str | None = None
    task_objective: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    known_exceptions: list[str] = Field(default_factory=list)
    waived_findings: list[str] = Field(default_factory=list)

    # Changed file payload (path → profile-scoped snippets or full content)
    files_changed: dict[str, str] = Field(default_factory=dict)

    # Unified diff (path → diff text)
    unified_diffs: dict[str, str] = Field(default_factory=dict)

    # Dependency context (nearby files, path → content excerpt)
    dependency_context: dict[str, str] = Field(default_factory=dict)

    # Governance rules (path → content)
    governance_rules: dict[str, str] = Field(default_factory=dict)

    # Deterministic check results
    test_results: list[DeterministicResult] = Field(default_factory=list)

    # Project metadata (tech stack, connector info, etc.)
    project_metadata: dict[str, Any] = Field(default_factory=dict)

    # Prior round findings (for resubmit context)
    prior_round_findings: list[Finding] = Field(default_factory=list)
    prior_round_number: int | None = None

    # Provenance
    diff_hash: str = ""
    governance_version: str = ""
    prompt_version: str = ""
    connector_type: str = "filesystem"
    connector_capabilities: list[str] = Field(default_factory=list)
    completion_gates: list[str] = Field(default_factory=list)
