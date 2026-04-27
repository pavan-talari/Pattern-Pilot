"""Memory store — all database writes go through here (single responsibility)."""

from __future__ import annotations

import logging
from typing import Any

from pattern_pilot.core.config import pp_now

from sqlalchemy.ext.asyncio import AsyncSession

from pattern_pilot.core.contracts import (
    DeterministicResult,
    Finding as FindingContract,
    FindingTier,
    ReviewRoundResult,
    ReviewStatus,
    Verdict,
)
from pattern_pilot.db import models

logger = logging.getLogger(__name__)


class MemoryStore:
    """Writes QC state to Pattern Pilot's database."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── Review Runs ──────────────────────────────────────────────────────

    async def create_run(
        self,
        project_id: str,
        task_ref: str,
        review_profile: str,
        governance_snapshot: dict[str, Any],
        prompt_version: str,
        diff_hash: str | None = None,
        connector_type: str = "filesystem",
        connector_capabilities: list[str] | None = None,
        task_id: str | None = None,
        decision_id: str | None = None,
        attempt_number: int | None = None,
    ) -> models.ReviewRun:
        """Create a new review run."""
        run = models.ReviewRun(
            project_id=project_id,
            task_ref=task_ref,
            task_id=task_id,
            decision_id=decision_id,
            attempt_number=attempt_number,
            status=ReviewStatus.RUNNING.value,
            review_profile=review_profile,
            governance_snapshot=governance_snapshot,
            prompt_version=prompt_version,
            diff_hash=diff_hash,
            connector_type=connector_type,
            connector_capabilities=connector_capabilities or [],
            started_at=pp_now(),
        )
        self.session.add(run)
        await self.session.flush()
        await self._log_event(run.project_id, run.id, "run_created", {
            "task_ref": task_ref,
            "review_profile": review_profile,
        })
        return run

    async def complete_run(
        self,
        run: models.ReviewRun,
        status: ReviewStatus,
        verdict: Verdict | None = None,
    ) -> None:
        """Mark a run as completed."""
        run.status = status.value
        run.verdict = verdict.value if verdict else None
        run.completed_at = pp_now()
        await self.session.flush()
        await self._log_event(run.project_id, run.id, "run_completed", {
            "status": status.value,
            "verdict": verdict.value if verdict else None,
            "total_rounds": run.total_rounds,
        })

    # ── Submissions ──────────────────────────────────────────────────────

    async def record_submission(
        self,
        run_id: str | None,
        submission_number: int,
        diff_hash: str,
        files_changed: list[str],
        deterministic_results: list[DeterministicResult],
        deterministic_passed: bool,
        progressed_to_llm: bool,
    ) -> models.ReviewSubmission:
        """Record a submit_for_review call."""
        submission = models.ReviewSubmission(
            run_id=run_id,
            submission_number=submission_number,
            diff_hash=diff_hash,
            files_changed=files_changed,
            deterministic_results=[r.model_dump() for r in deterministic_results],
            deterministic_passed=deterministic_passed,
            progressed_to_llm=progressed_to_llm,
        )
        self.session.add(submission)
        await self.session.flush()
        return submission

    # ── Rounds ───────────────────────────────────────────────────────────

    async def record_round(
        self,
        run: models.ReviewRun,
        round_result: ReviewRoundResult,
        request_payload: dict[str, Any] | None = None,
        create_advisories: bool = False,
    ) -> models.ReviewRound:
        """Record a single LLM review round."""
        rnd = models.ReviewRound(
            run_id=run.id,
            round_number=round_result.round_number,
            request_payload=request_payload or {},
            response_payload={
                "verdict": round_result.verdict.value,
                "findings_count": len(round_result.findings),
            },
            verdict=round_result.verdict.value,
            model_used=round_result.model_used,
            tokens_in=round_result.tokens_in,
            tokens_out=round_result.tokens_out,
            cost_usd=round_result.cost_usd,
            duration_ms=round_result.duration_ms,
        )
        self.session.add(rnd)
        await self.session.flush()

        # Record findings
        for finding in round_result.findings:
            db_finding = await self._record_finding(rnd.id, run.id, finding)
            if create_advisories and finding.tier == FindingTier.ADVISORY:
                await self.create_advisory(
                    project_id=run.project_id,
                    task_ref=run.task_ref,
                    finding=finding,
                    finding_id=db_finding.id,
                )

        run.total_rounds = max(run.total_rounds or 0, round_result.round_number)
        await self.session.flush()

        await self._log_event(run.project_id, run.id, "round_completed", {
            "round_number": round_result.round_number,
            "verdict": round_result.verdict.value,
            "findings_count": len(round_result.findings),
        })

        return rnd

    # ── Findings ─────────────────────────────────────────────────────────

    async def _record_finding(
        self,
        round_id: str,
        run_id: str,
        finding: FindingContract,
    ) -> models.Finding:
        """Write a single finding to the DB."""
        db_finding = models.Finding(
            round_id=round_id,
            run_id=run_id,
            tier=finding.tier.value,
            category=finding.category,
            file_path=finding.file_path,
            line_start=finding.line_start,
            line_end=finding.line_end,
            message=finding.message,
            suggestion=finding.suggestion,
            autofix_safe=finding.autofix_safe,
            severity=finding.severity.value,
            confidence=finding.confidence,
            rule_refs=finding.rule_refs,
            why_now=finding.why_now,
            autofix_diff=finding.autofix_diff,
            status=finding.status.value,
        )
        self.session.add(db_finding)
        await self.session.flush()
        return db_finding

    # ── Advisories ───────────────────────────────────────────────────────

    async def create_advisory(
        self,
        project_id: str,
        task_ref: str,
        finding: FindingContract,
        finding_id: str,
    ) -> models.Advisory:
        """Create a Tier-3 advisory."""
        advisory = models.Advisory(
            project_id=project_id,
            task_ref=task_ref,
            finding_id=finding_id,
            message=finding.message,
            category=finding.category,
        )
        self.session.add(advisory)
        await self.session.flush()
        return advisory

    # ── Event Log ────────────────────────────────────────────────────────

    async def _log_event(
        self,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Append to the audit trail."""
        event = models.EventLog(
            project_id=project_id,
            run_id=run_id,
            event_type=event_type,
            payload=payload,
        )
        self.session.add(event)
        await self.session.flush()
