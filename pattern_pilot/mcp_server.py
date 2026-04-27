"""MCP server — Claude Desktop integration point.

Runs on HOST (not Docker). Uses async submit + poll architecture:
- submit_for_review: returns run_id immediately (<3s), review runs in background
- get_review_status: polls for results, returns findings when complete
- cancel_review: force-expire a stuck run
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
import traceback
from datetime import timedelta
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from sqlalchemy import and_, or_, select

from pattern_pilot.core.config import get_settings, pp_now
from pattern_pilot.core.contracts import (
    ReviewProfile,
    ReviewStatus,
    SubmitRequest,
)
from pattern_pilot.core.orchestrator import Orchestrator
from pattern_pilot.db import models as db_models
from pattern_pilot.db.session import AsyncSessionLocal
from pattern_pilot.governance.loader import GovernanceLoader

logger = logging.getLogger(__name__)

server = Server("pattern-pilot")

# Track background review tasks by run_id
_background_tasks: dict[str, asyncio.Task[None]] = {}

# Stale-run timeout: auto-expire runs stuck in "running" longer than this
STALE_RUN_TIMEOUT_MINUTES = 5


def _truncate_check_output(output: str, max_chars: int = 1200) -> str:
    text = (output or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _format_deterministic_failure(payload: dict[str, Any]) -> list[str]:
    """Render deterministic check failures as target-project issues, not PP infra errors."""
    checks = payload.get("checks") or []
    if not checks:
        return [
            "\n**Failure type:** Target project deterministic checks failed before LLM review.",
            "No deterministic check details were recorded for this run.",
            "This is not a Pattern Pilot infrastructure failure.",
        ]

    lines = [
        "\n**Failure type:** Target project deterministic checks failed before LLM review.",
        "This is not a Pattern Pilot infrastructure failure.",
        "\n**Failed checks:**",
    ]
    for check in checks:
        if check.get("passed", False):
            continue
        check_name = check.get("check_name", "unknown")
        duration_ms = check.get("duration_ms", 0)
        lines.append(f"- `{check_name}` failed ({duration_ms}ms)")
        output = _truncate_check_output(str(check.get("output", "")))
        if output:
            indented = textwrap.indent(output, "  ")
            lines.append(f"  Output:\n```\n{indented}\n```")
    if len(lines) == 3:
        lines.append("- One or more deterministic checks failed, but no failing outputs were captured.")
    return lines


def _format_round_limit_reached(payload: dict[str, Any]) -> list[str]:
    """Explain that the run escalated before a new resubmit review was attempted."""
    max_rounds = payload.get("max_rounds", "configured")
    last_completed_round = payload.get("last_completed_round", 0)
    return [
        "\n**Escalation reason:** Max review rounds were already exhausted before this resubmit could be reviewed.",
        f"- Configured round limit: `{max_rounds}`",
        f"- Last completed round: `{last_completed_round}`",
        "- No new LLM round was run for this resubmit.",
        "- Any findings shown below are from the last completed round, not from the latest code submission.",
    ]


def _format_reviewer_error(payload: dict[str, Any]) -> list[str]:
    error = payload.get("error", "Reviewer infrastructure unavailable.")
    return [
        "\n**Failure type:** Reviewer infrastructure error.",
        f"- Error: {error}",
        "- The target code was not reviewed.",
        "- This run is retryable on the same task without creating a fresh review history.",
    ]


# ── Tool definitions ─────────────────────────────────────────────────────────


@server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="submit_for_review",
            description=(
                "Submit code for QC review. Returns a run_id immediately (within 2-3 seconds). "
                "The review runs in the background — poll with get_review_status to retrieve "
                "results. This avoids MCP transport timeouts on large files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_name": {
                        "type": "string",
                        "description": "Name of the onboarded project",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Stable task identity (e.g., 'TASK-653'). Used for run continuity across retries. If omitted, falls back to task_ref.",
                    },
                    "task_ref": {
                        "type": "string",
                        "description": "Human-readable task label. Display only — does not affect run lookup.",
                    },
                    "decision_id": {
                        "type": "string",
                        "description": "Groups tasks under a shared change stream (e.g., 'DEC-302'). Optional.",
                    },
                    "attempt_number": {
                        "type": "integer",
                        "description": "Which attempt this is (1, 2, 3...). Metadata only — never creates a new run.",
                    },
                    "files_changed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional changed file paths (relative). Leave empty with use_git_diff=true for auto-discovery.",
                    },
                    "use_git_diff": {
                        "type": "boolean",
                        "description": "When true, PP can auto-discover changed files from git diff.",
                        "default": False,
                    },
                    "diff_base": {
                        "type": "string",
                        "description": "Git base ref for diff mode (default HEAD).",
                        "default": "HEAD",
                    },
                    "diff_scope": {
                        "type": "string",
                        "enum": ["unstaged", "staged", "all"],
                        "description": "Git diff scope for auto-discovery and payload diffs.",
                        "default": "unstaged",
                    },
                    "review_profile": {
                        "type": "string",
                        "enum": ["quick", "standard", "deep"],
                        "description": "Review depth: quick (diff only), standard (+ governance), deep (+ deps)",
                        "default": "standard",
                    },
                    "task_objective": {
                        "type": "string",
                        "description": "What this task is trying to achieve. Optional — auto-resolved from docs/tasks/{task_id}.md if omitted.",
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Acceptance criteria. Optional — auto-resolved from task doc if omitted.",
                    },
                    "decision_summary": {
                        "type": "string",
                        "description": "Decision summary. Optional — auto-resolved from docs/decisions/{decision_id}.md if omitted.",
                    },
                    "known_exceptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Known exceptions/tradeoffs. Optional — auto-resolved from decision doc if omitted.",
                    },
                    "waived_findings": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Finding messages to treat as waived. Optional — auto-resolved from task doc if omitted.",
                    },
                },
                "required": ["project_name", "task_ref"],
            },
        ),
        Tool(
            name="get_review_status",
            description=(
                "Check the status of a review run. Returns verdict and findings once "
                "the review completes. Poll every 10-15 seconds after submit_for_review."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "The review run ID returned by submit_for_review",
                    },
                },
                "required": ["run_id"],
            },
        ),
        Tool(
            name="list_runs",
            description=(
                "List run history for a stable task identity. Useful after context "
                "compaction to recover run IDs and outcomes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Stable task identity (e.g., 'TASK-716').",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Optional project filter (e.g., 'story-engine').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max runs to return (default 20, max 100).",
                        "default": 20,
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="cancel_review",
            description=(
                "Force-cancel a stuck review run. Marks it as failed so new reviews "
                "can be submitted."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "The review run ID to cancel",
                    },
                },
                "required": ["run_id"],
            },
        ),
        Tool(
            name="list_projects",
            description="List all onboarded projects.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_advisories",
            description="Get recent advisories for a project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_name": {
                        "type": "string",
                        "description": "Name of the project",
                    },
                },
                "required": ["project_name"],
            },
        ),
    ]


# ── Tool handlers ────────────────────────────────────────────────────────────


@server.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "submit_for_review":
        return await _handle_submit(arguments)
    elif name == "get_review_status":
        return await _handle_status(arguments)
    elif name == "list_runs":
        return await _handle_list_runs(arguments)
    elif name == "cancel_review":
        return await _handle_cancel(arguments)
    elif name == "list_projects":
        return await _handle_list_projects()
    elif name == "get_advisories":
        return await _handle_advisories(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ── Submit (async — returns immediately) ─────────────────────────────────────


async def _handle_submit(args: dict[str, Any]) -> list[TextContent]:
    """Handle submit_for_review — fast path.

    Creates the run in DB (or finds existing for resubmit), starts background
    review, returns run_id immediately.
    """
    profile_str = args.get("review_profile", "standard")
    try:
        profile = ReviewProfile(profile_str)
    except ValueError:
        profile = ReviewProfile.STANDARD

    # Build request with stable identity fields
    task_id = args.get("task_id") or args["task_ref"]  # Fallback: task_ref as task_id
    # Build request. For list fields, use None when omitted so the merge
    # logic can distinguish "not provided" (→ filesystem fallback) from
    # "explicitly empty []" (→ intentional clear, no fallback).
    request = SubmitRequest(
        project_name=args["project_name"],
        task_ref=args["task_ref"],
        task_id=task_id,
        decision_id=args.get("decision_id"),
        attempt_number=args.get("attempt_number"),
        files_changed=args.get("files_changed", []),
        review_profile=profile,
        use_git_diff=bool(args.get("use_git_diff", False)),
        diff_base=str(args.get("diff_base", "HEAD") or "HEAD"),
        diff_scope=str(args.get("diff_scope", "unstaged") or "unstaged"),
        decision_summary=args.get("decision_summary"),
        task_objective=args.get("task_objective"),
        acceptance_criteria=args.get("acceptance_criteria"),
        known_exceptions=args.get("known_exceptions"),
        waived_findings=args.get("waived_findings"),
    )

    async with AsyncSessionLocal() as session:
        orchestrator = Orchestrator(session)

        # Validate project exists
        project = await orchestrator._load_project(request.project_name)
        if not project:
            return [TextContent(
                type="text",
                text=f"Project '{request.project_name}' not found. Onboard it first.",
            )]

        connector = orchestrator._build_connector(project)
        request.files_changed = await orchestrator._resolve_files_changed(
            connector=connector,
            files_changed=request.files_changed,
            use_git_diff=request.use_git_diff,
            diff_base=request.diff_base,
            diff_scope=request.diff_scope,
        )
        if not request.files_changed and request.use_git_diff:
            return [TextContent(
                type="text",
                text=(
                    "No changed files detected from git diff. "
                    f"(base={request.diff_base}, scope={request.diff_scope})"
                ),
            )]
        if not request.files_changed and not request.use_git_diff:
            return [TextContent(
                type="text",
                text=(
                    "No changed files provided. "
                    "When use_git_diff=false, files_changed is required."
                ),
            )]

        # Check for existing active run using STABLE task_id (not mutable task_ref)
        lookup_key = request.task_id or request.task_ref
        existing_run = await orchestrator._find_active_run(
            project.id, lookup_key
        )

        # Guard: check if ANY run is currently running for this project.
        # Auto-expire stale runs (running with 0 rounds for > timeout).
        any_running = (await session.execute(
            select(db_models.ReviewRun).where(
                db_models.ReviewRun.project_id == project.id,
                db_models.ReviewRun.status == "running",
            )
        )).scalar_one_or_none()

        if any_running and (not existing_run or any_running.id != existing_run.id):
            # Check if the running run is stale
            stale_cutoff = pp_now() - timedelta(minutes=STALE_RUN_TIMEOUT_MINUTES)
            is_stale = (
                any_running.started_at
                and any_running.started_at < stale_cutoff
                and (any_running.total_rounds or 0) == 0
            )

            if is_stale:
                # Auto-expire the stale run
                any_running.status = "failed"
                any_running.verdict = "failed"
                any_running.completed_at = pp_now()
                stale_event = db_models.EventLog(
                    project_id=project.id,
                    run_id=any_running.id,
                    event_type="run_auto_expired",
                    payload={
                        "reason": f"Stale run: running with 0 rounds for >{STALE_RUN_TIMEOUT_MINUTES}min",
                        "started_at": str(any_running.started_at),
                    },
                )
                session.add(stale_event)
                await session.commit()
                logger.warning(
                    "Auto-expired stale run %s (started %s, 0 rounds)",
                    any_running.id, any_running.started_at,
                )
                # Also cancel the background task if it exists
                bg_task = _background_tasks.pop(str(any_running.id), None)
                if bg_task and not bg_task.done():
                    bg_task.cancel()
            else:
                return [TextContent(
                    type="text",
                    text=(
                        f"**A review is already running for this project.**\n"
                        f"**Active Run ID:** {any_running.id}\n"
                        f"**Task:** {any_running.task_ref}\n"
                        f"**Started:** {any_running.started_at}\n"
                        f"**Rounds so far:** {any_running.total_rounds or 0}\n\n"
                        f"Wait for it to complete, or cancel with "
                        f"`cancel_review(run_id=\"{any_running.id}\")` if stuck."
                    ),
                )]

        if existing_run:
            # Resubmit: move from blocked back to running, update mutable fields.
            # A resubmit may supply IDs that the original submission lacked.
            run_id = str(existing_run.id)
            existing_run.status = ReviewStatus.RUNNING.value
            existing_run.task_ref = request.task_ref  # Update display label
            if request.task_id and not existing_run.task_id:
                existing_run.task_id = request.task_id
            if request.decision_id and not existing_run.decision_id:
                existing_run.decision_id = request.decision_id
            if request.attempt_number:
                existing_run.attempt_number = request.attempt_number
            await session.commit()
            logger.info("Resubmit: run %s moved from blocked to running (task_id=%s)", run_id, task_id)
        else:
            # Create a new run with stable identity
            governance_loader = GovernanceLoader(connector)
            snapshot = await governance_loader.load(project.governance_paths or [])

            run = await orchestrator.store.create_run(
                project_id=project.id,
                task_ref=request.task_ref,
                task_id=request.task_id,
                decision_id=request.decision_id,
                attempt_number=request.attempt_number,
                review_profile=request.review_profile.value,
                governance_snapshot=snapshot.model_dump(mode="json"),
                prompt_version=orchestrator.settings.pp_prompt_version,
                diff_hash=orchestrator._compute_diff_hash(request.files_changed),
                connector_type=project.connector_type,
                connector_capabilities=[
                    c.value for c in connector.get_info().capabilities
                ],
            )
            run_id = str(run.id)
            await session.commit()

    # Build task context dict to pass through to execute_round → reviewer
    task_context = {
        "task_id": request.task_id,
        "decision_id": request.decision_id,
        "attempt_number": request.attempt_number,
        "decision_summary": request.decision_summary,
        "task_objective": request.task_objective,
        "acceptance_criteria": request.acceptance_criteria,
        "known_exceptions": request.known_exceptions,
        "waived_findings": request.waived_findings,
    }

    # Fire background review task — uses execute_round, NOT submit_for_review
    task = asyncio.create_task(
        _run_review_background(
            run_id,
            request.files_changed,
            profile,
            task_context,
            use_git_diff=request.use_git_diff,
            diff_base=request.diff_base,
            diff_scope=request.diff_scope,
        )
    )
    _background_tasks[run_id] = task

    # Clean up completed tasks
    for rid in list(_background_tasks):
        if _background_tasks[rid].done():
            del _background_tasks[rid]

    return [TextContent(
        type="text",
        text=(
            f"**Review queued.**\n"
            f"**Run ID:** {run_id}\n"
            f"**Status:** running\n"
            f"**Files:** {', '.join(request.files_changed)}\n"
            f"**Profile:** {profile.value}\n\n"
            f"Poll with `get_review_status(run_id=\"{run_id}\")` to check results."
        ),
    )]


async def _run_review_background(
    run_id: str,
    files_changed: list[str],
    review_profile: ReviewProfile,
    task_context: dict[str, Any] | None = None,
    use_git_diff: bool = False,
    diff_base: str = "HEAD",
    diff_scope: str = "unstaged",
) -> None:
    """Background task — runs a single review round via execute_round.

    Uses the Orchestrator.execute_round() method which works with an
    already-created run_id.  Intermediate state (submissions, rounds)
    is committed inside execute_round, so partial progress survives
    even if a later step fails.
    """
    try:
        async with AsyncSessionLocal() as session:
            orchestrator = Orchestrator(session)
            response = await orchestrator.execute_round(
                run_id=run_id,
                files_changed=files_changed,
                review_profile=review_profile,
                task_context=task_context,
                use_git_diff=use_git_diff,
                diff_base=diff_base,
                diff_scope=diff_scope,
            )
            # Final commit (execute_round commits intermediate state,
            # but there may be minor pending changes)
            await session.commit()
            logger.info(
                "Background review completed: run=%s status=%s verdict=%s",
                run_id,
                response.status.value,
                response.verdict.value if response.verdict else "none",
            )
    except Exception as exc:
        logger.exception("Background review failed for run %s", run_id)
        # Mark run as failed and log error details to event_log
        try:
            async with AsyncSessionLocal() as err_session:
                run = await err_session.get(db_models.ReviewRun, run_id)
                if run and run.status in ("running", "pending"):
                    run.status = "failed"
                    run.verdict = "failed"
                    run.completed_at = pp_now()
                    error_event = db_models.EventLog(
                        project_id=run.project_id,
                        run_id=run_id,
                        event_type="run_failed",
                        payload={
                            "error": str(exc),
                            "traceback": traceback.format_exc()[-2000:],
                        },
                    )
                    err_session.add(error_event)
                    await err_session.commit()
                    logger.info("Marked run %s as failed after error", run_id)
        except Exception:
            logger.exception("Failed to mark run %s as failed", run_id)


# ── Status (returns findings when complete) ──────────────────────────────────


async def _handle_status(args: dict[str, Any]) -> list[TextContent]:
    """Handle get_review_status — returns full details including findings."""
    run_id = args["run_id"]

    async with AsyncSessionLocal() as session:
        run = await session.get(db_models.ReviewRun, run_id)
        if not run:
            return [TextContent(type="text", text=f"Review run '{run_id}' not found.")]

        # Terminal states — include full findings
        terminal = {
            "passed",
            "passed_with_advisories",
            "blocked",
            "escalated",
            "failed",
            "reviewer_error",
            "abandoned",
        }
        is_complete = run.status in terminal

        parts = [
            f"**Run ID:** {run.id}",
            f"**Task:** {run.task_ref}",
            f"**Status:** {run.status}",
            f"**Verdict:** {run.verdict or (run.status if run.status == 'reviewer_error' else 'pending')}",
            f"**Rounds:** {run.total_rounds}",
            f"**Submissions:** {run.total_submissions}",
        ]

        if not is_complete:
            # Still running — tell caller to poll again
            elapsed = ""
            if run.started_at:
                delta = pp_now() - run.started_at
                elapsed = f" (elapsed: {int(delta.total_seconds())}s)"
            parts.append(f"\n**Review in progress.**{elapsed} Poll again in 10-15 seconds.")
            return [TextContent(type="text", text="\n".join(parts))]

        # For failed runs, include the latest error from event_log
        if run.status == "failed":
            error_result = await session.execute(
                select(db_models.EventLog)
                .where(
                    db_models.EventLog.run_id == run_id,
                    db_models.EventLog.event_type.in_(["run_failed", "run_auto_expired"]),
                )
                .order_by(db_models.EventLog.created_at.desc())
                .limit(1)
            )
            error_event = error_result.scalar_one_or_none()
            if error_event and error_event.payload:
                payload = error_event.payload
                if payload.get("phase") == "deterministic_checks":
                    parts.extend(_format_deterministic_failure(payload))
                else:
                    error_msg = payload.get("error", "Unknown error")
                    reason = payload.get("reason", "")
                    if reason:
                        parts.append(f"\n**Failure reason:** {reason}")
                    else:
                        parts.append(f"\n**Error:** {error_msg}")
            else:
                parts.append("\n**Error:** Unknown error")
        elif run.status == "reviewer_error":
            error_result = await session.execute(
                select(db_models.EventLog)
                .where(
                    db_models.EventLog.run_id == run_id,
                    db_models.EventLog.event_type == "run_reviewer_error",
                )
                .order_by(db_models.EventLog.created_at.desc())
                .limit(1)
            )
            error_event = error_result.scalar_one_or_none()
            if error_event and error_event.payload:
                parts.extend(_format_reviewer_error(error_event.payload))
            else:
                parts.extend(_format_reviewer_error({}))
        elif run.status == "escalated":
            limit_result = await session.execute(
                select(db_models.EventLog)
                .where(
                    db_models.EventLog.run_id == run_id,
                    db_models.EventLog.event_type == "run_round_limit_reached",
                )
                .order_by(db_models.EventLog.created_at.desc())
                .limit(1)
            )
            limit_event = limit_result.scalar_one_or_none()
            if limit_event and limit_event.payload:
                parts.extend(_format_round_limit_reached(limit_event.payload))

        # Fetch findings for the LATEST round only (not all rounds)
        # This prevents showing stale round-1 findings after a resubmit failure
        latest_round = None
        if run.total_rounds and run.total_rounds > 0:
            round_result = await session.execute(
                select(db_models.ReviewRound)
                .where(
                    db_models.ReviewRound.run_id == run_id,
                    db_models.ReviewRound.round_number == run.total_rounds,
                )
            )
            latest_round = round_result.scalar_one_or_none()

        if latest_round:
            result = await session.execute(
                select(db_models.Finding)
                .where(db_models.Finding.round_id == latest_round.id)
                .order_by(db_models.Finding.created_at)
            )
            findings = result.scalars().all()
        else:
            # Fallback: all findings for the run
            result = await session.execute(
                select(db_models.Finding)
                .where(db_models.Finding.run_id == run_id)
                .order_by(db_models.Finding.created_at)
            )
            findings = result.scalars().all()

        if findings:
            round_label = f" (round {run.total_rounds})" if run.total_rounds else ""
            parts.append(f"\n**Findings ({len(findings)}){round_label}:**")
            for i, f in enumerate(findings, 1):
                tier_label = f.tier.upper() if f.tier else "UNKNOWN"
                loc = f.file_path or ""
                if f.line_start:
                    loc += f":{f.line_start}"
                parts.append(f"  {i}. [{tier_label}] {loc} — {f.message}")
                if f.suggestion:
                    parts.append(f"     Suggestion: {f.suggestion}")
                if f.severity:
                    parts.append(f"     Severity: {f.severity} | Confidence: {f.confidence or 0:.0%}")
                if f.why_now:
                    parts.append(f"     Why now: {f.why_now}")
                if f.autofix_safe:
                    parts.append("     (autofix safe)")
                if f.autofix_diff:
                    parts.append(f"     Autofix diff:\n```\n{f.autofix_diff}\n```")
        else:
            if run.status == "failed":
                parts.append("\nNo LLM findings were recorded because review did not reach round 1.")
            elif run.status == "reviewer_error":
                parts.append("\nNo findings were recorded because the reviewer was unavailable.")
            else:
                parts.append("\nNo findings — clean pass.")

        # Action guidance
        if run.status == "blocked":
            parts.append("\n**Action required:** Fix the blocking findings and resubmit.")
        elif run.status == "escalated":
            parts.append("\n**Escalated for human review.** Manual verification required.")
        elif run.status in ("passed", "passed_with_advisories"):
            parts.append("\n**Review complete.** Code approved.")
        elif run.status == "failed":
            parts.append("\n**Review failed before LLM review completed.** Fix the reported issue and submit a new review.")
        elif run.status == "reviewer_error":
            parts.append("\n**Reviewer unavailable.** Retry this task later; the same run can continue on resubmit.")

        return [TextContent(type="text", text="\n".join(parts))]


# ── Cancel ───────────────────────────────────────────────────────────────────


async def _handle_cancel(args: dict[str, Any]) -> list[TextContent]:
    """Handle cancel_review — force-expire a stuck run."""
    run_id = args["run_id"]

    async with AsyncSessionLocal() as session:
        run = await session.get(db_models.ReviewRun, run_id)
        if not run:
            return [TextContent(type="text", text=f"Review run '{run_id}' not found.")]

        if run.status not in ("running", "pending"):
            return [TextContent(
                type="text",
                text=f"Run '{run_id}' is already in terminal state: {run.status}. Nothing to cancel.",
            )]

        run.status = "failed"
        run.verdict = "failed"
        run.completed_at = pp_now()
        cancel_event = db_models.EventLog(
            project_id=run.project_id,
            run_id=run_id,
            event_type="run_cancelled",
            payload={"reason": "Manual cancellation via cancel_review"},
        )
        session.add(cancel_event)
        await session.commit()

    # Cancel the background task if it exists
    bg_task = _background_tasks.pop(run_id, None)
    if bg_task and not bg_task.done():
        bg_task.cancel()

    return [TextContent(
        type="text",
        text=(
            f"**Run cancelled.**\n"
            f"**Run ID:** {run_id}\n"
            f"**Status:** failed (cancelled)\n\n"
            f"You can now submit a new review."
        ),
    )]


# ── Other handlers ───────────────────────────────────────────────────────────


async def _handle_list_projects() -> list[TextContent]:
    """Handle list_projects."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(db_models.Project)
            .where(db_models.Project.archived_at.is_(None))
            .order_by(db_models.Project.name)
        )
        projects = result.scalars().all()

    if not projects:
        return [TextContent(type="text", text="No projects onboarded yet.")]

    lines = ["**Onboarded Projects:**"]
    for p in projects:
        lines.append(f"- **{p.name}** ({p.connector_type}) — {p.repo_path}")
    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_advisories(args: dict[str, Any]) -> list[TextContent]:
    """Handle get_advisories."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(db_models.Project).where(
                db_models.Project.name == args["project_name"],
                db_models.Project.archived_at.is_(None),
            )
        )
        project = result.scalar_one_or_none()
        if not project:
            return [TextContent(type="text", text=f"Project '{args['project_name']}' not found.")]

        adv_result = await session.execute(
            select(db_models.Advisory)
            .where(
                db_models.Advisory.project_id == project.id,
                db_models.Advisory.status == "active",
            )
            .order_by(db_models.Advisory.created_at.desc())
            .limit(20)
        )
        advisories = adv_result.scalars().all()

    if not advisories:
        return [TextContent(type="text", text=f"No active advisories for '{args['project_name']}'.")]

    lines = [f"**Active Advisories for {args['project_name']}:**"]
    for a in advisories:
        lines.append(f"- [{a.category}] {a.message} (task: {a.task_ref})")
    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_list_runs(args: dict[str, Any]) -> list[TextContent]:
    """Handle list_runs."""
    task_id = str(args.get("task_id", "")).strip()
    if not task_id:
        return [TextContent(type="text", text="`task_id` is required.")]
    project_name = args.get("project_name")
    raw_limit = args.get("limit", 20)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    async with AsyncSessionLocal() as session:
        query = (
            select(db_models.ReviewRun, db_models.Project.name)
            .join(db_models.Project, db_models.Project.id == db_models.ReviewRun.project_id)
            .where(
                or_(
                    db_models.ReviewRun.task_id == task_id,
                    and_(
                        db_models.ReviewRun.task_id.is_(None),
                        db_models.ReviewRun.task_ref == task_id,
                    ),
                )
            )
            .order_by(db_models.ReviewRun.created_at.desc())
            .limit(limit)
        )
        if project_name:
            query = query.where(db_models.Project.name == project_name)
        rows = (await session.execute(query)).all()

    if not rows:
        scope = f" in project '{project_name}'" if project_name else ""
        return [TextContent(type="text", text=f"No runs found for task_id '{task_id}'{scope}.")]

    header = f"**Run history for {task_id}**"
    if project_name:
        header += f" in **{project_name}**"
    lines = [header]
    for run, proj_name in rows:
        verdict = run.verdict or (
            "reviewer_error" if run.status == "reviewer_error" else "pending"
        )
        created = run.created_at.isoformat() if run.created_at else "n/a"
        lines.append(
            f"- `{run.id}` | `{proj_name}` | status=`{run.status}` | verdict=`{verdict}` "
            f"| rounds={run.total_rounds} | submissions={run.total_submissions} | created={created}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


# ── Entry point ──────────────────────────────────────────────────────────────


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.pp_log_level, logging.INFO))
    logger.info("Pattern Pilot MCP server starting (async mode)...")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
