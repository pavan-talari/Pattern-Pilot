"""Orchestrator — the QC feedback loop.

submit → deterministic checks → build context → LLM review → verdict
If BLOCKING or recommended_autofix → return findings → await resubmit
If PASS / PASS_WITH_ADVISORIES → log, exit
If rounds exhausted → REQUIRES_HUMAN_REVIEW → escalate
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pattern_pilot.checks.runner import CheckRunner
from pattern_pilot.connectors.base import BaseConnector
from pattern_pilot.connectors.filesystem import FilesystemConnector
from pattern_pilot.context.bundle_builder import BundleBuilder
from pattern_pilot.context.context_resolver import ContextResolver
from pattern_pilot.core.config import get_settings, pp_now
from pattern_pilot.core.contracts import (
    Finding,
    FindingSeverity,
    FindingTier,
    ReviewProfile,
    ReviewRoundResult,
    ReviewStatus,
    SubmitRequest,
    SubmitResponse,
    Verdict,
)
from pattern_pilot.core.reviewer import Reviewer, ReviewerError
from pattern_pilot.db import models
from pattern_pilot.governance.loader import GovernanceLoader
from pattern_pilot.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class Orchestrator:
    """Entry point for the QC review loop."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()
        self.store = MemoryStore(session)

    async def submit_for_review(self, request: SubmitRequest) -> SubmitResponse:
        """Main entry point — called by MCP or API."""

        # Step 1: Load project
        project = await self._load_project(request.project_name)
        if not project:
            return SubmitResponse(
                run_id="",
                status=ReviewStatus.FAILED,
                message=f"Project '{request.project_name}' not found. Onboard it first.",
            )

        # Step 2: Build connector (resolves Docker↔host path)
        connector = self._build_connector(project)
        resolved_files_changed = await self._resolve_files_changed(
            connector=connector,
            files_changed=request.files_changed,
            use_git_diff=request.use_git_diff,
            diff_base=request.diff_base,
            diff_scope=request.diff_scope,
        )
        if not resolved_files_changed:
            if request.use_git_diff:
                return SubmitResponse(
                    run_id="",
                    status=ReviewStatus.FAILED,
                    message=(
                        "No changed files detected from git diff. "
                        f"(base={request.diff_base}, scope={request.diff_scope})"
                    ),
                )
            return SubmitResponse(
                run_id="",
                status=ReviewStatus.FAILED,
                message=(
                    "No changed files provided. "
                    "When use_git_diff=false, files_changed is required."
                ),
            )

        # Step 3: Deterministic checks
        resolved_path = self.settings.resolve_repo_path(project.repo_path)
        check_runner = CheckRunner(
            resolved_path,
            files_changed=resolved_files_changed,
        )
        det_results = await check_runner.run_all()
        det_passed = CheckRunner.all_passed(det_results)

        # Compute diff hash
        diff_hash = self._compute_diff_hash(resolved_files_changed)

        # Check for existing run (resubmit scenario)
        # Use stable task_id for lookup; fall back to task_ref for compat
        lookup_key = request.task_id or request.task_ref
        run = await self._find_active_run(project.id, lookup_key, lock=True)
        is_resubmit = run is not None
        submission_number = (run.total_submissions + 1) if run else 1

        # Record submission
        await self.store.record_submission(
            run_id=run.id if run else None,
            submission_number=submission_number,
            diff_hash=diff_hash,
            files_changed=resolved_files_changed,
            deterministic_results=det_results,
            deterministic_passed=det_passed,
            progressed_to_llm=det_passed,
        )

        # If deterministic checks fail, return immediately (no LLM round consumed)
        if not det_passed:
            if run:
                await self._mark_run_deterministic_failure(run, det_results)
            return SubmitResponse(
                run_id=run.id if run else "",
                status=ReviewStatus.FAILED,
                message="Deterministic checks failed. Fix these before review.",
                findings=[],
                requires_resubmit=True,
            )

        # Step 4: Create or update run
        if not run:
            governance_loader = GovernanceLoader(connector)
            snapshot = await governance_loader.load(project.governance_paths or [])
            run = await self.store.create_run(
                project_id=project.id,
                task_ref=request.task_ref,
                task_id=request.task_id,
                decision_id=request.decision_id,
                attempt_number=request.attempt_number,
                review_profile=request.review_profile.value,
                governance_snapshot=snapshot.model_dump(mode="json"),
                prompt_version=self.settings.pp_prompt_version,
                diff_hash=diff_hash,
                connector_type=project.connector_type,
                connector_capabilities=[
                    c.value for c in connector.get_info().capabilities
                ],
            )

        run.total_submissions = submission_number
        # Update display label and identity fields on resubmit.
        # A resubmit may supply IDs that the original submission lacked.
        run.task_ref = request.task_ref
        if request.task_id and not run.task_id:
            run.task_id = request.task_id
        if request.decision_id and not run.decision_id:
            run.decision_id = request.decision_id
        if request.attempt_number:
            run.attempt_number = request.attempt_number
        # Resubmit: move from blocked back to running
        if run.status == ReviewStatus.BLOCKED.value:
            run.status = ReviewStatus.RUNNING.value

        # Step 5: Check round limit
        round_number = run.total_rounds + 1
        max_rounds = self.settings.pp_max_rounds

        if round_number > max_rounds:
            await self._mark_run_round_limit_reached(run, max_rounds)
            await self.store.complete_run(
                run, ReviewStatus.ESCALATED, Verdict.REQUIRES_HUMAN_REVIEW
            )
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.ESCALATED,
                verdict=Verdict.REQUIRES_HUMAN_REVIEW,
                round_number=run.total_rounds,
                message=(
                    f"Max rounds ({max_rounds}) exhausted before reviewing this resubmit. "
                    "No new LLM round was run."
                ),
            )

        # Step 5b: Load prior-round findings for resubmit context
        prior_findings: list[Finding] = []
        prior_round_num: int | None = None
        if is_resubmit and round_number > 1:
            prior_findings, prior_round_num = await self._load_prior_round_findings(
                run.id, round_number - 1
            )

        # Step 6: Build context bundle
        governance_loader = GovernanceLoader(connector)
        bundle_builder = BundleBuilder(connector, governance_loader)
        bundle = await bundle_builder.build(
            project_name=project.name,
            task_ref=request.task_ref,
            files_changed=resolved_files_changed,
            review_profile=request.review_profile,
            governance_paths=project.governance_paths or [],
            test_results=det_results,
            project_metadata=project.tech_stack or {},
            run_id=run.id,
            round_number=round_number,
            diff_hash=diff_hash,
            governance_version=run.governance_snapshot.get("captured_at", ""),
            prompt_version=self.settings.pp_prompt_version,
            connector_type=project.connector_type,
            connector_capabilities=[
                c.value for c in connector.get_info().capabilities
            ],
            completion_gates=list(project.completion_gates.keys())
            if project.completion_gates
            else [],
            diff_base=request.diff_base,
            diff_scope=request.diff_scope,
        )

        # Inject prior-round findings into bundle
        if prior_findings:
            bundle.prior_round_findings = prior_findings
            bundle.prior_round_number = prior_round_num
            logger.info(
                "[PRIOR-CTX] Injected %d findings from round %d for run %s",
                len(prior_findings), prior_round_num, run.id,
            )
            for pf in prior_findings:
                logger.info("[PRIOR-CTX]   - %s: %s", pf.tier.value, pf.message[:80])
        else:
            logger.info(
                "[PRIOR-CTX] No prior findings loaded (is_resubmit=%s, round=%d)",
                is_resubmit, round_number,
            )

        # Step 6b: Inject task context (stable identity + filesystem resolution)
        effective_task_id = request.task_id or run.task_id
        effective_decision_id = request.decision_id or run.decision_id

        # Build submission context dict.
        # None = omitted by caller (filesystem fallback applies).
        # Empty string/list = intentionally cleared (preserved, no fallback).
        submit_ctx: dict[str, Any] = {
            "task_id": effective_task_id,
            "decision_id": effective_decision_id,
            "attempt_number": request.attempt_number or run.attempt_number,
            "decision_summary": request.decision_summary,
            "task_objective": request.task_objective,
        }
        # Only include list fields if the caller explicitly provided them
        # (even if empty). None = omitted → key stays absent → filesystem fills.
        if request.acceptance_criteria is not None:
            submit_ctx["acceptance_criteria"] = request.acceptance_criteria
        if request.known_exceptions is not None:
            submit_ctx["known_exceptions"] = request.known_exceptions
        if request.waived_findings is not None:
            submit_ctx["waived_findings"] = request.waived_findings

        # Filesystem context resolution — fills blanks from markdown docs.
        # A field is "missing" if absent from ctx or explicitly None.
        # Empty string or empty list = intentional override, NOT overwritten.
        if effective_decision_id or effective_task_id:
            resolved = await self._resolve_context_from_filesystem(
                connector, project, effective_decision_id, effective_task_id
            )
            if resolved:
                for key, value in resolved.items():
                    if value and (key not in submit_ctx or submit_ctx[key] is None):
                        submit_ctx[key] = value

        bundle.task_id = effective_task_id
        bundle.decision_id = effective_decision_id
        bundle.attempt_number = submit_ctx.get("attempt_number")
        bundle.decision_summary = submit_ctx.get("decision_summary")
        bundle.task_objective = submit_ctx.get("task_objective")
        bundle.acceptance_criteria = submit_ctx.get("acceptance_criteria") or []
        bundle.known_exceptions = submit_ctx.get("known_exceptions") or []
        bundle.waived_findings = submit_ctx.get("waived_findings") or []

        # Step 6c: Recompute content-based diff hash now that we have file content
        content_hash = self._compute_content_hash(
            bundle.files_changed, bundle.unified_diffs
        )
        run.diff_hash = content_hash
        bundle.diff_hash = content_hash

        # Step 7: Send to reviewer
        try:
            reviewer = self._build_reviewer(project)
            round_result = await reviewer.review(bundle)
        except ReviewerError as exc:
            await self._mark_run_reviewer_error(run, str(exc))
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.REVIEWER_ERROR,
                round_number=round_number,
                message=(
                    "Reviewer infrastructure was unavailable before code could be evaluated. "
                    f"Reason: {exc}"
                ),
                requires_resubmit=True,
            )
        round_result.round_number = round_number

        # Step 7b: Apply iteration policy (Phase 1) — BEFORE persisting
        if prior_findings:
            round_result = self._apply_iteration_policy(
                round_result, prior_findings, round_number
            )

        # Step 8: Record round
        await self.store.record_round(
            run,
            round_result,
            create_advisories=round_result.verdict == Verdict.PASS_WITH_ADVISORIES,
        )

        # Step 9: Evaluate verdict
        verdict = round_result.verdict

        if verdict == Verdict.PASS:
            await self.store.complete_run(run, ReviewStatus.PASSED, Verdict.PASS)
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.PASSED,
                verdict=Verdict.PASS,
                round_number=round_number,
                message="Clean pass. No findings.",
            )

        if verdict == Verdict.PASS_WITH_ADVISORIES:
            await self.store.complete_run(
                run, ReviewStatus.PASSED_WITH_ADVISORIES, Verdict.PASS_WITH_ADVISORIES
            )
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.PASSED_WITH_ADVISORIES,
                verdict=Verdict.PASS_WITH_ADVISORIES,
                round_number=round_number,
                findings=round_result.findings,
                message="Passed with advisories. Tier 2b surfaced, Tier 3 logged.",
            )

        # REQUIRES_HUMAN_REVIEW — escalate, do not loop
        if verdict == Verdict.REQUIRES_HUMAN_REVIEW:
            await self.store.complete_run(
                run, ReviewStatus.ESCALATED, Verdict.REQUIRES_HUMAN_REVIEW
            )
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.ESCALATED,
                verdict=Verdict.REQUIRES_HUMAN_REVIEW,
                round_number=round_number,
                findings=round_result.findings,
                requires_resubmit=False,
                message=(
                    f"Round {round_number}: Escalated for human review. "
                    "Unresolved architectural or policy ambiguity."
                ),
            )

        # BLOCKING — set status to BLOCKED, return findings for Claude to fix
        blocking_findings = [
            f
            for f in round_result.findings
            if f.tier in (FindingTier.BLOCKING, FindingTier.RECOMMENDED_AUTOFIX)
        ]
        run.status = ReviewStatus.BLOCKED.value
        return SubmitResponse(
            run_id=run.id,
            status=ReviewStatus.BLOCKED,
            verdict=Verdict.BLOCKING,
            round_number=round_number,
            findings=round_result.findings,
            requires_resubmit=True,
            message=f"Round {round_number}: {len(blocking_findings)} blocking finding(s). Fix and resubmit.",
        )

    # ── execute_round (called by MCP background task) ──────────────────

    async def execute_round(
        self,
        run_id: str,
        files_changed: list[str],
        review_profile: ReviewProfile,
        task_context: dict[str, Any] | None = None,
        use_git_diff: bool = False,
        diff_base: str = "HEAD",
        diff_scope: str = "unstaged",
    ) -> SubmitResponse:
        """Execute a single review round for an already-created run.

        This is the method called by the MCP background task. Unlike
        submit_for_review, it does NOT create or search for runs — it
        works with a specific run_id that was already created by the
        MCP _handle_submit.  Intermediate state is committed so that
        progress (submissions, rounds, findings) is never rolled back
        by a late-stage failure.
        """

        # ── Load run + project + health check ───────────────────────
        run = await self._load_run_for_update(run_id)
        if not run:
            return SubmitResponse(
                run_id=run_id,
                status=ReviewStatus.FAILED,
                message=f"Run '{run_id}' not found in database.",
            )

        project = await self.session.get(models.Project, run.project_id)
        if not project:
            return SubmitResponse(
                run_id=run_id,
                status=ReviewStatus.FAILED,
                message=f"Project for run '{run_id}' not found.",
            )

        # ── Connector + filesystem health check ──────────────────────
        connector = self._build_connector(project)
        resolved_files_changed = await self._resolve_files_changed(
            connector=connector,
            files_changed=files_changed,
            use_git_diff=use_git_diff,
            diff_base=diff_base,
            diff_scope=diff_scope,
        )
        if not resolved_files_changed:
            await self._mark_run_failed(
                run,
                (
                    "No changed files detected from git diff."
                    if use_git_diff
                    else "No changed files provided for review submission."
                ),
                "input_validation",
            )
            await self.session.commit()
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.FAILED,
                message=(
                    "No changed files detected from git diff."
                    if use_git_diff
                    else "No changed files provided. When use_git_diff=false, files_changed is required."
                ),
                requires_resubmit=True,
            )

        # Fail fast if the host filesystem is unresponsive (e.g., Mac asleep)
        if hasattr(connector, "check_health"):
            healthy, health_msg = await connector.check_health(timeout=10.0)
            if not healthy:
                logger.error(
                    "[HEALTH] Filesystem unreachable for run %s: %s",
                    run_id, health_msg,
                )
                run.status = ReviewStatus.FAILED.value
                run.verdict = "failed"
                run.completed_at = pp_now()
                await self.store._log_event(
                    run.project_id, run_id, "run_failed",
                    {"error": health_msg, "phase": "health_check"},
                )
                await self.session.commit()
                return SubmitResponse(
                    run_id=run_id,
                    status=ReviewStatus.FAILED,
                    message=f"Filesystem health check failed: {health_msg}",
                )

        # ── Deterministic checks ────────────────────────────────────
        resolved_path = self.settings.resolve_repo_path(project.repo_path)
        check_runner = CheckRunner(
            resolved_path,
            files_changed=resolved_files_changed,
        )
        det_results = await check_runner.run_all()
        det_passed = CheckRunner.all_passed(det_results)

        diff_hash = self._compute_diff_hash(resolved_files_changed)
        submission_number = (run.total_submissions or 0) + 1

        await self.store.record_submission(
            run_id=run.id,
            submission_number=submission_number,
            diff_hash=diff_hash,
            files_changed=resolved_files_changed,
            deterministic_results=det_results,
            deterministic_passed=det_passed,
            progressed_to_llm=det_passed,
        )
        run.total_submissions = submission_number

        if not det_passed:
            await self._mark_run_deterministic_failure(run, det_results)
            await self.session.commit()
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.FAILED,
                message="Deterministic checks failed. Fix these before review.",
                requires_resubmit=True,
            )

        # ── Round limit check ───────────────────────────────────────
        round_number = (run.total_rounds or 0) + 1
        max_rounds = self.settings.pp_max_rounds

        if round_number > max_rounds:
            await self._mark_run_round_limit_reached(run, max_rounds)
            await self.store.complete_run(
                run, ReviewStatus.ESCALATED, Verdict.REQUIRES_HUMAN_REVIEW
            )
            await self.session.commit()
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.ESCALATED,
                verdict=Verdict.REQUIRES_HUMAN_REVIEW,
                round_number=run.total_rounds,
                message=(
                    f"Max rounds ({max_rounds}) exhausted before reviewing this resubmit. "
                    "No new LLM round was run."
                ),
            )

        # ── Prior-round findings (resubmit context) ─────────────────
        prior_findings: list[Finding] = []
        prior_round_num: int | None = None
        if round_number > 1:
            prior_findings, prior_round_num = await self._load_prior_round_findings(
                run.id, round_number - 1
            )
            logger.info(
                "[PRIOR-CTX] Injected %d findings from round %d for run %s",
                len(prior_findings), round_number - 1, run.id,
            )

        # ── Build context bundle ────────────────────────────────────
        governance_loader = GovernanceLoader(connector)
        bundle_builder = BundleBuilder(connector, governance_loader)
        bundle = await bundle_builder.build(
            project_name=project.name,
            task_ref=run.task_ref,
            files_changed=resolved_files_changed,
            review_profile=review_profile,
            governance_paths=project.governance_paths or [],
            test_results=det_results,
            project_metadata=project.tech_stack or {},
            run_id=run.id,
            round_number=round_number,
            diff_hash=diff_hash,
            governance_version=run.governance_snapshot.get("captured_at", ""),
            prompt_version=self.settings.pp_prompt_version,
            connector_type=project.connector_type,
            connector_capabilities=[
                c.value for c in connector.get_info().capabilities
            ],
            completion_gates=list(project.completion_gates.keys())
            if project.completion_gates
            else [],
            diff_base=diff_base,
            diff_scope=diff_scope,
        )

        # Inject prior-round findings
        if prior_findings:
            bundle.prior_round_findings = prior_findings
            bundle.prior_round_number = prior_round_num

        # Inject task context (stable identity + decision/task metadata)
        # Phase 2: resolve from filesystem if IDs present but content fields empty
        ctx = task_context or {}
        effective_task_id = ctx.get("task_id") or run.task_id
        effective_decision_id = ctx.get("decision_id") or run.decision_id

        # If we have IDs, try filesystem resolution to fill any missing fields.
        # Submission payload takes precedence — filesystem fills blanks only.
        # A field is "missing" if absent from ctx or explicitly None.
        # Empty string or empty list = intentional override, NOT overwritten.
        if effective_decision_id or effective_task_id:
            resolved = await self._resolve_context_from_filesystem(
                connector, project, effective_decision_id, effective_task_id
            )
            if resolved:
                for key, value in resolved.items():
                    if value and (key not in ctx or ctx[key] is None):
                        ctx[key] = value

        bundle.task_id = effective_task_id
        bundle.decision_id = effective_decision_id
        bundle.attempt_number = ctx.get("attempt_number") or run.attempt_number
        bundle.decision_summary = ctx.get("decision_summary")
        bundle.task_objective = ctx.get("task_objective")
        bundle.acceptance_criteria = ctx.get("acceptance_criteria") or []
        bundle.known_exceptions = ctx.get("known_exceptions") or []
        bundle.waived_findings = ctx.get("waived_findings") or []

        # Recompute content-based diff hash
        content_hash = self._compute_content_hash(
            bundle.files_changed, bundle.unified_diffs
        )
        run.diff_hash = content_hash
        bundle.diff_hash = content_hash

        # ── Call reviewer ──────────────────────────────────────────
        try:
            reviewer = self._build_reviewer(project)
            round_result = await reviewer.review(bundle)
        except ReviewerError as exc:
            await self._mark_run_reviewer_error(run, str(exc))
            await self.session.commit()
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.REVIEWER_ERROR,
                round_number=round_number,
                message=(
                    "Reviewer infrastructure was unavailable before code could be evaluated. "
                    f"Reason: {exc}"
                ),
                requires_resubmit=True,
            )
        round_result.round_number = round_number

        # ── Repeated-finding policy (Phase 1 Gap 3) ─────────────────
        # Applied BEFORE persisting so DB state matches the operational outcome.
        # On round 3+, auto-downgrade medium/low-severity blocking
        # findings that appear to be repeated from prior rounds.
        if round_number >= 3 and prior_findings:
            round_result = self._apply_iteration_policy(
                round_result, prior_findings, round_number
            )

        # ── Record round (commit immediately) ───────────────────────
        await self.store.record_round(
            run,
            round_result,
            create_advisories=round_result.verdict == Verdict.PASS_WITH_ADVISORIES,
        )
        await self.session.commit()

        # ── Evaluate verdict ────────────────────────────────────────
        verdict = round_result.verdict

        if verdict == Verdict.PASS:
            await self.store.complete_run(run, ReviewStatus.PASSED, Verdict.PASS)
            await self.session.commit()
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.PASSED,
                verdict=Verdict.PASS,
                round_number=round_number,
                message="Clean pass. No findings.",
            )

        if verdict == Verdict.PASS_WITH_ADVISORIES:
            await self.store.complete_run(
                run, ReviewStatus.PASSED_WITH_ADVISORIES, Verdict.PASS_WITH_ADVISORIES
            )
            await self.session.commit()
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.PASSED_WITH_ADVISORIES,
                verdict=Verdict.PASS_WITH_ADVISORIES,
                round_number=round_number,
                findings=round_result.findings,
                message="Passed with advisories.",
            )

        if verdict == Verdict.REQUIRES_HUMAN_REVIEW:
            await self.store.complete_run(
                run, ReviewStatus.ESCALATED, Verdict.REQUIRES_HUMAN_REVIEW
            )
            await self.session.commit()
            return SubmitResponse(
                run_id=run.id,
                status=ReviewStatus.ESCALATED,
                verdict=Verdict.REQUIRES_HUMAN_REVIEW,
                round_number=round_number,
                findings=round_result.findings,
                message=f"Round {round_number}: Escalated for human review.",
            )

        # BLOCKING — set status to BLOCKED, return findings for Claude to fix
        blocking_findings = [
            f
            for f in round_result.findings
            if f.tier in (FindingTier.BLOCKING, FindingTier.RECOMMENDED_AUTOFIX)
        ]
        run.status = ReviewStatus.BLOCKED.value
        await self.session.commit()
        return SubmitResponse(
            run_id=run.id,
            status=ReviewStatus.BLOCKED,
            verdict=Verdict.BLOCKING,
            round_number=round_number,
            findings=round_result.findings,
            requires_resubmit=True,
            message=f"Round {round_number}: {len(blocking_findings)} blocking finding(s). Fix and resubmit.",
        )

    # ── Iteration policy ───────────────────────────────────────────────

    @staticmethod
    def _apply_iteration_policy(
        round_result: ReviewRoundResult,
        prior_findings: list[Finding],
        round_number: int,
    ) -> ReviewRoundResult:
        """Auto-downgrade repeated weak findings on round 3+.

        Policy:
        - Round 3+: medium/low severity blocking findings that match a
          prior finding (same file + similar message) are downgraded to
          recommended_review (non-blocking).
        - High severity findings are NEVER downgraded.
        - If all blocking findings are downgraded, verdict becomes
          PASS_WITH_ADVISORIES.
        """
        # Only apply on round 3+
        if round_number < 3:
            return round_result

        prior_signatures = set()
        for pf in prior_findings:
            # Signature: file_path + first 60 chars of message (fuzzy match)
            sig = f"{pf.file_path}::{pf.message[:60]}"
            prior_signatures.add(sig)

        downgraded = 0
        for f in round_result.findings:
            if f.tier not in (FindingTier.BLOCKING, FindingTier.RECOMMENDED_AUTOFIX):
                continue
            if f.severity == FindingSeverity.HIGH:
                continue  # Never downgrade high severity

            sig = f"{f.file_path}::{f.message[:60]}"
            if sig in prior_signatures:
                logger.info(
                    "[POLICY] Downgrading repeated finding (round %d, severity=%s): %s",
                    round_number, f.severity.value, f.message[:80],
                )
                f.tier = FindingTier.RECOMMENDED_REVIEW
                downgraded += 1

        if downgraded > 0:
            # Recompute verdict — if no blocking findings remain, pass with advisories
            still_blocking = [
                f for f in round_result.findings
                if f.tier in (FindingTier.BLOCKING, FindingTier.RECOMMENDED_AUTOFIX)
            ]
            if not still_blocking:
                round_result.verdict = Verdict.PASS_WITH_ADVISORIES
                logger.info(
                    "[POLICY] All blocking findings downgraded on round %d. Verdict → PASS_WITH_ADVISORIES",
                    round_number,
                )

        return round_result

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _load_project(self, project_name: str) -> models.Project | None:
        """Load project by name."""
        result = await self.session.execute(
            select(models.Project).where(
                models.Project.name == project_name,
                models.Project.archived_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    def _build_reviewer(self, project: models.Project) -> Reviewer:
        """Build the reviewer using project-level config with env fallbacks."""
        provider = project.reviewer_provider or self.settings.openai_default_provider
        return Reviewer(
            provider=provider,
            model=project.reviewer_model or self.settings.reviewer_default_model(provider),
            reasoning_effort=(
                project.reviewer_reasoning_effort
                or self.settings.openai_reasoning_effort
            ),
        )

    @staticmethod
    def _normalize_files_changed(files_changed: list[str]) -> list[str]:
        """Normalize, dedupe, and keep order for changed file paths."""
        seen: set[str] = set()
        normalized: list[str] = []
        for raw_path in files_changed:
            path = (raw_path or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            normalized.append(path)
        return normalized

    async def _resolve_files_changed(
        self,
        connector: BaseConnector,
        files_changed: list[str],
        use_git_diff: bool,
        diff_base: str,
        diff_scope: str,
    ) -> list[str]:
        """Resolve final changed-file set with optional git auto-discovery."""
        explicit = self._normalize_files_changed(files_changed)
        if explicit:
            return explicit
        if not use_git_diff:
            return explicit
        try:
            discovered = await connector.list_changed_files(
                diff_base=diff_base,
                diff_scope=diff_scope,
            )
        except Exception as exc:
            logger.warning(
                "Git diff auto-discovery failed (base=%s scope=%s): %s",
                diff_base,
                diff_scope,
                exc,
            )
            return explicit
        return self._normalize_files_changed(discovered)

    async def _load_run_for_update(self, run_id: str) -> models.ReviewRun | None:
        """Load a run with a row lock so resubmit counters cannot race."""
        result = await self.session.execute(
            select(models.ReviewRun)
            .where(models.ReviewRun.id == run_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _find_active_run(
        self, project_id: str, task_id: str, lock: bool = False
    ) -> models.ReviewRun | None:
        """Find an active (running or blocked) review run for this task.

        Uses task_id (stable identity) for lookup. Falls back to task_ref
        matching for backward compatibility with runs created before v1.3.
        """
        # Primary lookup: by task_id
        stmt = select(models.ReviewRun).where(
            models.ReviewRun.project_id == project_id,
            models.ReviewRun.task_id == task_id,
            models.ReviewRun.status.in_([
                ReviewStatus.RUNNING.value,
                ReviewStatus.BLOCKED.value,
                ReviewStatus.REVIEWER_ERROR.value,
            ]),
        )
        if lock:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        run = result.scalar_one_or_none()
        if run:
            return run

        # Backward compat fallback: by task_ref (for pre-v1.3 runs with no task_id)
        stmt = select(models.ReviewRun).where(
            models.ReviewRun.project_id == project_id,
            models.ReviewRun.task_ref == task_id,
            models.ReviewRun.task_id.is_(None),
            models.ReviewRun.status.in_([
                ReviewStatus.RUNNING.value,
                ReviewStatus.BLOCKED.value,
                ReviewStatus.REVIEWER_ERROR.value,
            ]),
        )
        if lock:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _mark_run_failed(
        self,
        run: models.ReviewRun,
        error: str,
        phase: str,
    ) -> None:
        """Persist an infrastructure failure that prevented a usable review."""
        run.status = ReviewStatus.FAILED.value
        run.verdict = "failed"
        run.completed_at = pp_now()
        await self.store._log_event(
            run.project_id,
            run.id,
            "run_failed",
            {"phase": phase, "error": error},
        )
        await self.session.flush()

    async def _mark_run_deterministic_failure(
        self,
        run: models.ReviewRun,
        deterministic_results: list[Any],
    ) -> None:
        """Persist deterministic gate failures with audit detail."""
        failed_checks = [
            result.model_dump()
            for result in deterministic_results
            if not result.passed
        ]
        run.status = ReviewStatus.FAILED.value
        run.verdict = "failed"
        run.completed_at = pp_now()
        await self.store._log_event(
            run.project_id,
            run.id,
            "run_failed",
            {
                "phase": "deterministic_checks",
                "checks": failed_checks,
            },
        )
        await self.session.flush()

    async def _mark_run_reviewer_error(
        self,
        run: models.ReviewRun,
        error: str,
    ) -> None:
        """Persist a reviewer infrastructure error without treating it as a code failure."""
        run.status = ReviewStatus.REVIEWER_ERROR.value
        run.verdict = None
        run.completed_at = pp_now()
        await self.store._log_event(
            run.project_id,
            run.id,
            "run_reviewer_error",
            {
                "phase": "reviewer",
                "error": error,
                "retryable": True,
            },
        )
        await self.session.flush()

    async def _mark_run_round_limit_reached(
        self,
        run: models.ReviewRun,
        max_rounds: int,
    ) -> None:
        """Record that a resubmit was escalated before a new review round could run."""
        await self.store._log_event(
            run.project_id,
            run.id,
            "run_round_limit_reached",
            {
                "max_rounds": max_rounds,
                "last_completed_round": run.total_rounds,
                "review_attempted": False,
            },
        )
        await self.session.flush()

    async def _resolve_context_from_filesystem(
        self,
        connector: BaseConnector,
        project: models.Project,
        decision_id: str | None,
        task_id: str | None,
    ) -> dict[str, Any] | None:
        """Resolve decision/task context from filesystem markdown files.

        Uses project config for directory conventions, falling back to defaults.
        Returns a dict matching the task_context format, or None if nothing resolved.
        """
        config = project.connector_config or {}
        decisions_dir = config.get("decisions_dir", "docs/decisions")
        tasks_dir = config.get("tasks_dir", "docs/tasks")

        resolver = ContextResolver(
            connector=connector,
            decisions_dir=decisions_dir,
            tasks_dir=tasks_dir,
        )
        resolved = await resolver.resolve(
            decision_id=decision_id,
            task_id=task_id,
        )

        result = resolved.as_dict()
        if result:
            logger.info(
                "[CONTEXT-RESOLVER] Resolved %d fields from filesystem for decision=%s task=%s",
                len(result), decision_id, task_id,
            )
        return result if result else None

    async def _load_prior_round_findings(
        self, run_id: str, prior_round: int
    ) -> tuple[list[Finding], int | None]:
        """Load findings from the previous round for resubmit context."""
        # Find the review round record
        result = await self.session.execute(
            select(models.ReviewRound).where(
                models.ReviewRound.run_id == run_id,
                models.ReviewRound.round_number == prior_round,
            )
        )
        round_record = result.scalar_one_or_none()
        if not round_record:
            return [], None

        # Load findings for that round (join through round_id)
        finding_result = await self.session.execute(
            select(models.Finding).where(
                models.Finding.round_id == round_record.id,
            )
        )
        db_findings = finding_result.scalars().all()

        findings = []
        for f in db_findings:
            findings.append(Finding(
                tier=FindingTier(f.tier) if f.tier else FindingTier.ADVISORY,
                category=f.category or "correctness",
                file_path=f.file_path or "unknown",
                line_start=f.line_start,
                line_end=f.line_end,
                message=f.message or "",
                suggestion=f.suggestion,
                autofix_safe=f.autofix_safe or False,
                severity=FindingSeverity(f.severity) if f.severity else FindingSeverity.MEDIUM,
                confidence=f.confidence or 0.8,
                rule_refs=f.rule_refs or [],
                why_now=f.why_now,
            ))

        return findings, prior_round

    def _build_connector(self, project: models.Project) -> BaseConnector:
        """Instantiate the appropriate connector for a project."""
        # Resolve path for current runtime (Docker vs host)
        resolved_path = self.settings.resolve_repo_path(project.repo_path)
        # For now, filesystem is the only generic connector.
        # Project-specific connectors are registered during onboarding.
        return FilesystemConnector(
            repo_path=resolved_path,
            config=project.connector_config or {},
        )

    @staticmethod
    def _compute_diff_hash(files_changed: list[str]) -> str:
        """Compute a preliminary hash from file paths.

        This is used before file content is available. Once the bundle
        is built, use _compute_content_hash for provenance-grade hashing.
        """
        content = "|".join(sorted(files_changed))
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    @staticmethod
    def _compute_content_hash(
        files_content: dict[str, str],
        diffs: dict[str, str] | None = None,
    ) -> str:
        """Hash actual file content + diffs for provenance-grade idempotency.

        Two different edits to the same files will produce different hashes.
        """
        h = hashlib.sha256()
        for path in sorted(files_content.keys()):
            h.update(path.encode())
            h.update(files_content[path].encode(errors="replace"))
        if diffs:
            for path in sorted(diffs.keys()):
                h.update(f"diff:{path}".encode())
                h.update(diffs[path].encode(errors="replace"))
        return h.hexdigest()[:32]
