"""OpenAI reviewer — sends context bundle via Responses API, receives structured findings.

Uses the OpenAI Responses API (not Chat Completions) with enforced JSON schema
output. Single model (gpt-5.4), reasoning medium.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, cast

import httpx
from openai import AsyncOpenAI

from pattern_pilot.core.config import get_settings
from pattern_pilot.core.contracts import (
    ContextBundle,
    Finding,
    FindingSeverity,
    FindingStatus,
    FindingTier,
    ReviewRoundResult,
    Verdict,
)

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL_ALIASES = {
    "claude-opus-4": "claude-opus-4-20250514",
    "claude-sonnet-4": "claude-sonnet-4-20250514",
}


class ReviewerError(RuntimeError):
    """Reviewer infrastructure failed before a usable review result was produced."""


class MalformedReviewerResponse(ReviewerError):
    """The reviewer returned output that did not match the expected JSON contract."""


# ── System instructions for the reviewer ─────────────────────────────────────

REVIEWER_INSTRUCTIONS = """\
You are the code review engine inside Pattern Pilot.

Your role:
You are a senior, careful QC reviewer. You review submitted code changes against:
1. the target project's own governance rules
2. project context
3. decision context
4. task context
5. deterministic check results
6. the submitted changed-code bundle

You do not write code. You do not patch files. You produce structured review \
findings only.

Source-of-truth rules:
Treat these sources as authoritative, in this order:
1. Changed files and unified diffs in the current submission
2. Filesystem-resolved task context for the submitted task_id
3. Filesystem-resolved decision context for the submitted decision_id
4. Governance files provided in the review bundle
5. Deterministic check results
6. Prior round findings and Pattern Pilot review memory

Do not invent repository facts, contracts, fields, or governance rules. If \
context is missing, say so through lower confidence and lower tiering rather \
than guessing.

Primary objective:
Help the coding agent complete the current task correctly with as few review \
cycles as possible. Your goal is not to maximize the number of findings. Your \
goal is to surface the highest-value, most grounded issues needed for task \
completion in one useful pass.

Review scope rules:
- Review the submitted change by default, not the whole repository.
- Use decision and task context to understand intent, acceptance criteria, \
constraints, and approved tradeoffs.
- Prefer issues introduced by the diff or made relevant by the diff.
- Do not flag unrelated legacy issues unless the submitted change directly \
depends on them, worsens them, or makes them unsafe.
- Do not drift into speculative hardening that is outside the task's actual \
objective.
- If the task context defines accepted exceptions, waived findings, or explicit \
tradeoffs, do not re-raise them unless the new code materially worsens the risk.

Task completion priority:
Prioritize findings in this order:
1. correctness and regressions
2. governance violations
3. contract / API mismatches
4. security and unsafe behavior
5. missing tests for changed behavior
6. performance issues with concrete impact
7. maintainability issues only when concrete and clearly relevant to the task
8. documentation issues only when they materially affect correctness or \
requirements

Decision-aware review behavior:
Use the decision context to understand architectural direction. Do not block \
the task for choices that are consistent with the stated decision, even if \
another design might also be reasonable. If the code appears to conflict with \
the decision context, explain the conflict clearly and cite the specific \
decision evidence.

Task-aware review behavior:
Use the task context to evaluate whether the implementation satisfies:
- task objective
- acceptance criteria
- stated constraints
- known exceptions

If the code satisfies the task objective and acceptance criteria, do not \
prolong the review loop with lower-value improvements unless they are clearly \
blocking quality or safety.

Finding tiers:
- blocking:
  Use only when the issue must be fixed before the current task should be \
considered acceptable. Examples: real bugs, broken contracts, governance \
violations, unsafe fallbacks, missing required migrations, missing required \
tests for changed behavior, materially misleading behavior.

- recommended_autofix:
  Use for local, low-risk, behavior-preserving fixes that are safe for an \
automated coding agent to apply. If you cannot describe a precise and safe \
fix, do not use this tier. For recommended_autofix findings, include the \
exact code change in autofix_diff as a unified diff patch. If you cannot \
produce a precise diff, downgrade to recommended_review.

- recommended_review:
  Use for human judgment, architecture tradeoffs, policy ambiguity, broader \
refactors, or medium-value concerns that should be reviewed but should not \
block by default.

- advisory:
  Use for long-term notes, patterns to watch, or future improvement ideas. \
Never use advisory for something that should actually block the task now.

Important tiering constraints:
- Do not downgrade real, high-confidence bugs into advisory.
- Do not upgrade personal preferences into blocking.
- Do not keep the task alive with low-value findings that are not necessary \
for the current task to be accepted.
- If an issue is ambiguous, medium/low severity, or weakly grounded, prefer \
recommended_review over blocking.
- On later rounds, be increasingly conservative about introducing new blocking \
findings unless they are clearly serious and directly relevant.

Severity guidance:
- HIGH: Data corruption, broken write-path contracts, security issues, \
required migration gaps, major governance violations, silent unsafe behavior.
- MEDIUM: Misleading behavior, missing validation, incorrect fallback \
behavior, task-relevant contract drift, missing targeted tests for changed \
behavior.
- LOW: Cosmetic issues, narrow maintainability concerns, non-critical wording, \
optional cleanups.
- For findings in read-only endpoints, diagnostic tools, CSV exports, or \
display-only paths: cap tier at recommended_autofix unless the display error \
could directly cause an operator to take a wrong action on a production system.

Blocking policy:
Use blocking only when all of the following are true:
1. the issue is grounded in the provided evidence
2. it is relevant to the current task
3. it materially affects correctness, safety, governance, or required acceptance
4. it is worth another review cycle

If any of those are not true, lower the tier.

Governance handling:
- When a finding maps to a provided governance rule, include the matching \
identifiers or file references in rule_refs.
- Only cite governance rules that were actually provided in the review bundle.
- Do not invent rule IDs, decision IDs, or policy names.
- If no governance rule applies, leave rule_refs empty.

Deterministic checks:
- Use deterministic check results as strong evidence.
- Do not restate noisy tool output without adding review value.
- Do not assume deterministic checks were run unless results are explicitly \
provided.

Cross-file contract validation:
When the review bundle includes dependency/import context, use it to validate \
imported contracts, signatures, constants, schemas, storage layouts, and path \
conventions. Do not invent cross-file facts beyond what is provided. If you \
cannot verify the contract from the provided evidence, lower confidence and \
lower the tier.

Security and reliability guidance:
Be especially alert for:
- secrets, credentials, or unsafe tokens
- missing validation on external or user-controlled input
- unsafe filesystem/path handling
- broad exception swallowing that hides failures
- silent fallback behavior that can mislead operators
- permission/auth checks missing from changed API paths

Test guidance:
- Flag missing tests only when changed behavior should reasonably be covered.
- Prefer targeted regression tests over broad test demands.
- Do not demand unrelated test expansion.

Completeness discipline:
- Scan the entire submitted change before returning. Do not drip findings \
one per round.
- If you find one instance of a bug class, actively search for ALL instances \
of that class in the submitted files before returning.
- Group related findings: if the same root cause manifests in multiple \
locations, return ONE finding with all affected locations listed, not \
separate findings per location.
- Aim to surface all blocking findings in a single round.

Grounding discipline:
- Do not assume the existence of dictionary keys, class fields, database \
columns, or API shapes unless they are visible in the provided files, diffs, \
or dependency context.
- If you cannot verify that a field, key, or method exists from the provided \
evidence, downgrade to recommended_review with a note that verification is \
needed.
- Never invent repository facts. If the bundle does not contain enough \
context to confirm an issue, say so explicitly and lower your confidence.

Resubmit protocol:
When prior round findings are present, this is a resubmit. Your first duty \
is verification, not fresh exploration.

On a resubmit:
- Verify each prior finding first.
- Confirm whether it is fixed, still present, or partially fixed.
- Only introduce new findings if they are genuinely introduced by the latest \
changes or were clearly visible and strongly grounded in the current submission.
- Do not drip-feed findings across rounds.
- Do not re-raise a prior finding without explicit evidence that the fix is \
incomplete or incorrect.

Iteration-awareness policy:
Pattern Pilot is optimizing for low iteration count. Starting on later rounds, \
apply a stricter standard before returning blocking findings. On round 3 and \
beyond:
- Only keep findings as blocking if they are high-severity, high-confidence, \
and clearly relevant to governance, contracts, correctness, or safety.
- Prefer recommended_review for repeated medium/low-severity concerns unless \
the evidence clearly shows the task is still unsafe or incomplete.
- Do not continue the loop for speculative hardening or weakly grounded concerns.

Waivers and accepted exceptions:
If the task context includes waived findings, known exceptions, or accepted \
tradeoffs:
- Do not re-raise them as blocking.
- Only mention them again if the new code broadens the impact or invalidates \
the basis for the waiver.

Verdict guidance:
- blocking: Use when there is at least one blocking finding that must be \
fixed. On round 3+, reserve blocking for data corruption, contract violations, \
or governance failures.
- requires_human_review: Use when unresolved human judgment is required — \
architecture ambiguity, policy conflict, or loop exhaustion.
- pass_with_advisories: Use when there are no blocking or recommended_autofix \
issues, but there are recommended_review or advisory findings.
- pass: Use only when there are no findings at all.

Output discipline:
- Be concise, precise, and evidence-based.
- Every finding must identify a concrete file and, when possible, a concrete \
line range.
- Explain what is wrong, why it matters now, and the practical fix direction.
- Group related manifestations under one finding when they share the same \
root cause.
- Do not return praise, prose summary, or commentary outside the schema.
- If the change is acceptable, return pass or pass_with_advisories as \
appropriate.

Final mindset:
Your job is to help the system finish the current task safely and correctly. \
Prefer one complete, grounded, high-value review over many incremental review \
cycles. Prefer no finding over a weak finding. Prefer task completion over \
speculative perfection.
"""

# ── Structured output schema ─────────────────────────────────────────────────

REVIEW_OUTPUT_SCHEMA = {
    "type": "json_schema",
    "name": "review_result",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [
                    "blocking",
                    "requires_human_review",
                    "pass_with_advisories",
                    "pass",
                ],
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tier": {
                            "type": "string",
                            "enum": [
                                "blocking",
                                "recommended_autofix",
                                "recommended_review",
                                "advisory",
                            ],
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "correctness",
                                "security",
                                "governance",
                                "architecture",
                                "testing",
                                "performance",
                                "maintainability",
                                "docs",
                            ],
                        },
                        "file_path": {"type": "string"},
                        "line_start": {"type": ["integer", "null"]},
                        "line_end": {"type": ["integer", "null"]},
                        "message": {"type": "string"},
                        "suggestion": {"type": ["string", "null"]},
                        "autofix_safe": {"type": "boolean"},
                        "severity": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "confidence": {"type": "number"},
                        "rule_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "why_now": {"type": ["string", "null"]},
                        "autofix_diff": {"type": ["string", "null"]},
                    },
                    "required": [
                        "tier",
                        "category",
                        "file_path",
                        "line_start",
                        "line_end",
                        "message",
                        "suggestion",
                        "autofix_safe",
                        "severity",
                        "confidence",
                        "rule_refs",
                        "why_now",
                        "autofix_diff",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["verdict", "findings"],
        "additionalProperties": False,
    },
}


class Reviewer:
    """Sends context bundles to supported reviewer APIs and parses findings."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        settings = get_settings()
        self.provider = provider or settings.openai_default_provider
        self.model = model or settings.reviewer_default_model(self.provider)
        self.reasoning_effort = reasoning_effort or settings.openai_reasoning_effort
        self.timeout_seconds = settings.reviewer_timeout_seconds(self.provider)
        self.base_url = settings.reviewer_base_url(self.provider)
        self.max_attempts = max(1, settings.openai_reviewer_max_attempts)
        self.retry_base_seconds = max(1, settings.openai_reviewer_retry_base_seconds)
        self.prompt_version = settings.pp_prompt_version
        self.client: AsyncOpenAI | None = None
        self.api_key = api_key or settings.reviewer_api_key(self.provider)

        if self.provider == "openai":
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            self.input_cost_per_1m = settings.openai_input_cost_per_1m
            self.output_cost_per_1m = settings.openai_output_cost_per_1m
        elif self.provider == "anthropic":
            self.model = ANTHROPIC_MODEL_ALIASES.get(self.model, self.model)
            self.input_cost_per_1m = None
            self.output_cost_per_1m = None
        else:
            raise ReviewerError(
                f"Reviewer provider runtime is not implemented yet: {self.provider}"
            )

        if not self.api_key:
            raise ReviewerError(
                f"Reviewer API key is not configured for provider: {self.provider}"
            )

        if self.provider == "openai" and (
            self.input_cost_per_1m is None or self.output_cost_per_1m is None
        ):
            logger.warning(
                "OpenAI cost rates are not configured; review cost_usd will be unavailable."
            )

    async def review(self, bundle: ContextBundle) -> ReviewRoundResult:
        """Send the context bundle for review via the configured provider API."""
        user_message = self._build_user_message(bundle)
        start = time.monotonic()
        last_exc: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self._request_review(user_message, attempt)
                break  # Success
            except Exception as exc:
                last_exc = exc
                elapsed_so_far = int(time.monotonic() - start)
                logger.warning(
                    "%s attempt %d/%d failed after %ds: %s",
                    self.provider.capitalize(),
                    attempt,
                    self.max_attempts,
                    elapsed_so_far,
                    exc,
                )
                if attempt < self.max_attempts:
                    wait = self.retry_base_seconds * attempt
                    logger.info("Retrying in %ds...", wait)
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "%s reviewer exhausted %d attempts",
                        self.provider.capitalize(),
                        self.max_attempts,
                    )
                    error_detail = (
                        f"{type(last_exc).__name__}: {last_exc}"
                        if last_exc
                        else "unknown reviewer transport error"
                    )
                    raise ReviewerError(
                        f"{self.provider.capitalize()} reviewer unavailable after "
                        f"{self.max_attempts} attempts. {error_detail}"
                    ) from exc

        elapsed = int((time.monotonic() - start) * 1000)

        # Extract structured output from response
        raw = self._extract_output_text(response)
        usage = self._extract_usage(response)
        tokens_in, tokens_out = self._extract_usage_tokens(usage)

        parsed, parse_ok = self._parse_response(raw)

        if not parse_ok:
            raise MalformedReviewerResponse(
                "Reviewer response could not be parsed as JSON. "
                f"Raw output (truncated): {raw[:300]}"
            )

        findings = parsed.get("findings", [])
        raw_verdict = parsed.get("verdict", "pass")

        # Map to domain objects
        finding_objects = [self._to_finding(f) for f in findings]
        verdict = self._to_verdict(raw_verdict, finding_objects)

        return ReviewRoundResult(
            round_number=0,  # Caller sets this
            verdict=verdict,
            findings=finding_objects,
            model_used=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._estimate_cost(tokens_in, tokens_out),
            duration_ms=elapsed,
        )

    async def _request_review(self, user_message: str, attempt: int) -> Any:
        """Dispatch the review request to the configured provider."""
        if self.provider == "openai":
            logger.info(
                "[REVIEWER] Sending to OpenAI (attempt %d/%d, timeout=%ds, effort=%s)",
                attempt,
                self.max_attempts,
                self.timeout_seconds,
                self.reasoning_effort,
            )
            create_response = cast(Any, self.client.responses.create)
            return await create_response(
                model=self.model,
                instructions=REVIEWER_INSTRUCTIONS,
                input=user_message,
                text={
                    "format": REVIEW_OUTPUT_SCHEMA,
                },
                reasoning={
                    "effort": self.reasoning_effort,
                },
                timeout=self.timeout_seconds,
            )

        if self.provider == "anthropic":
            logger.info(
                "[REVIEWER] Sending to Anthropic (attempt %d/%d, timeout=%ds)",
                attempt,
                self.max_attempts,
                self.timeout_seconds,
            )
            return await self._request_anthropic_review(user_message)

        raise ReviewerError(
            f"Reviewer provider runtime is not implemented yet: {self.provider}"
        )

    async def _request_anthropic_review(self, user_message: str) -> dict[str, Any]:
        """Call Anthropic Messages API and return the decoded JSON body."""
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "system": REVIEWER_INSTRUCTIONS,
            "messages": [
                {
                    "role": "user",
                    "content": user_message,
                }
            ],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self._anthropic_messages_url(),
                headers=headers,
                json=payload,
            )
        if response.status_code >= 400:
            detail = response.text.strip() or f"HTTP {response.status_code}"
            raise RuntimeError(f"Anthropic API error {response.status_code}: {detail}")
        return cast(dict[str, Any], response.json())

    @staticmethod
    def _extract_usage(response: Any) -> Any:
        """Return the usage payload across provider response shapes."""
        if isinstance(response, dict):
            return response.get("usage")
        return getattr(response, "usage", None)

    def _anthropic_messages_url(self) -> str:
        """Resolve the Anthropic Messages endpoint from the configured base URL."""
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1/messages"):
            return base_url
        if base_url.endswith("/v1"):
            return f"{base_url}/messages"
        return f"{base_url}/v1/messages"

    @staticmethod
    def _extract_usage_tokens(usage: Any) -> tuple[int, int]:
        """Read token counts across Responses and Chat Completions SDK shapes."""
        if not usage:
            return 0, 0
        if isinstance(usage, dict):
            return int(usage.get("input_tokens", 0) or 0), int(
                usage.get("output_tokens", 0) or 0
            )
        tokens_in = getattr(usage, "input_tokens", None)
        if tokens_in is None:
            tokens_in = getattr(usage, "prompt_tokens", 0)
        tokens_out = getattr(usage, "output_tokens", None)
        if tokens_out is None:
            tokens_out = getattr(usage, "completion_tokens", 0)
        return int(tokens_in or 0), int(tokens_out or 0)

    def _extract_output_text(self, response: Any) -> str:
        """Extract the text content from provider response shapes."""
        if isinstance(response, dict):
            content_blocks = response.get("content") or []
            texts = [
                str(block.get("text", ""))
                for block in content_blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "\n".join(text for text in texts if text) or "{}"
        if hasattr(response, "output"):
            for item in response.output:
                if hasattr(item, "type") and item.type == "message":
                    for content in item.content:
                        if hasattr(content, "text"):
                            return str(content.text)
        if hasattr(response, "output_text"):
            return str(response.output_text or "{}")
        return "{}"

    # ── User prompt template ─────────────────────────────────────────────

    def _build_user_message(self, bundle: ContextBundle) -> str:
        """Format the context bundle using the structured user prompt template."""
        parts: list[str] = []

        # ── Review metadata
        parts.append(
            "Review the submitted code change using the provided project, decision, "
            "and task context, governance rules, deterministic check results, and "
            "changed-code bundle. Return only JSON matching the required schema."
        )
        parts.append("")
        parts.append("## Review Metadata")
        parts.append(f"- Project: {bundle.project_name}")
        parts.append(f"- Task Ref: {bundle.task_ref}")
        parts.append(f"- Review Profile: {bundle.review_profile.value}")
        parts.append(f"- Run ID: {bundle.run_id}")
        parts.append(f"- Round Number: {bundle.round_number}")
        if bundle.task_id:
            parts.append(f"- Task ID: {bundle.task_id}")
        if bundle.decision_id:
            parts.append(f"- Decision ID: {bundle.decision_id}")
        if bundle.attempt_number:
            parts.append(f"- Attempt Number: {bundle.attempt_number}")
        parts.append("")

        # ── Project context (top of hierarchy)
        meta = bundle.project_metadata or {}
        parts.append("## Project Context")
        parts.append(f"- Connector Type: {bundle.connector_type}")
        parts.append(f"- Connector Capabilities: {', '.join(bundle.connector_capabilities) or 'none'}")
        parts.append(f"- Tech Stack: {json.dumps(meta.get('languages', []))}")
        parts.append(f"- Diff Hash: {bundle.diff_hash or 'n/a'}")
        parts.append(f"- Governance Version: {bundle.governance_version or 'n/a'}")
        parts.append(f"- Prompt Version: {bundle.prompt_version or 'n/a'}")
        parts.append("")

        # ── Decision context (middle of hierarchy)
        if bundle.decision_id or bundle.decision_summary or bundle.known_exceptions:
            parts.append("## Decision Context")
            if bundle.decision_id:
                parts.append(f"**Decision ID:** {bundle.decision_id}")
            if bundle.decision_summary:
                parts.append(f"**Summary:** {bundle.decision_summary}")
            parts.append(
                "Use the decision context to understand architectural direction. "
                "Do not block for choices consistent with the stated decision."
            )
            if bundle.known_exceptions:
                parts.append("**Known accepted exceptions (do not re-raise unless risk widens):**")
                for exc in bundle.known_exceptions:
                    parts.append(f"- {exc}")
            parts.append("")

        # ── Task context (bottom of hierarchy — most specific)
        if bundle.task_id or bundle.task_objective or bundle.acceptance_criteria or bundle.waived_findings:
            parts.append("## Task Context")
            if bundle.task_id:
                parts.append(f"**Task ID:** {bundle.task_id}")
            if bundle.task_objective:
                parts.append(f"**Objective:** {bundle.task_objective}")
            parts.append(
                "Evaluate whether the implementation satisfies the task objective "
                "and acceptance criteria. If it does, do not prolong the loop with "
                "lower-value improvements."
            )
            if bundle.acceptance_criteria:
                parts.append("**Acceptance criteria:**")
                for ac in bundle.acceptance_criteria:
                    parts.append(f"- {ac}")
            if bundle.waived_findings:
                parts.append("**Waived findings (do not re-raise):**")
                for wf in bundle.waived_findings:
                    parts.append(f"- {wf}")
            parts.append("")

        # ── Completion gates
        if bundle.completion_gates:
            parts.append("## Completion Gates")
            for gate in bundle.completion_gates:
                parts.append(f"- {gate}")
            parts.append("")

        # ── Deterministic check results
        if bundle.test_results:
            parts.append("## Deterministic Check Results")
            for tr in bundle.test_results:
                status = "PASS" if tr.passed else "FAIL"
                parts.append(f"- {tr.check_name}: {status}")
                if tr.output and not tr.passed:
                    # Include relevant failure output, truncated
                    parts.append(f"  - summary: {tr.output[:2000]}")
            parts.append("")

        # ── Prior round findings (BEFORE file content for high-attention position)
        if bundle.prior_round_findings:
            n = len(bundle.prior_round_findings)
            parts.append(
                f"## CRITICAL: Previous Round Findings (Round {bundle.prior_round_number}) — {n} finding(s)"
            )
            parts.append(
                "RESUBMIT PROTOCOL: The developer claims to have fixed these findings. "
                "For EACH prior finding below, you MUST:\n"
                "1. Locate the relevant code in the Changed Files section\n"
                "2. Determine if the fix adequately addresses the issue\n"
                "3. If FIXED: Do not re-report it. Move on.\n"
                "4. If NOT FIXED or PARTIALLY FIXED: Re-report with specific evidence "
                "showing what remains broken (cite the exact line and explain why the "
                "fix is inadequate).\n"
                "5. NEW findings in the same area must be clearly distinguished from "
                "prior findings.\n\n"
                "If you re-report a prior finding without citing specific evidence that "
                "the fix is inadequate, this is a review quality failure."
            )
            parts.append("")
            for i, pf in enumerate(bundle.prior_round_findings, 1):
                parts.append(f"### Prior Finding {i}")
                parts.append(f"- **Tier:** {pf.tier.value}")
                parts.append(f"- **File:** {pf.file_path}")
                if pf.line_start:
                    parts.append(f"- **Lines:** {pf.line_start}-{pf.line_end or pf.line_start}")
                parts.append(f"- **Message:** {pf.message}")
                if pf.suggestion:
                    parts.append(f"- **Suggestion:** {pf.suggestion}")
                parts.append("")
            logger.info(
                "[REVIEWER] User message includes Prior Round Findings section (%d findings)",
                n,
            )
        elif bundle.round_number > 1:
            logger.info("[REVIEWER] No prior-round findings in bundle (round %d)", bundle.round_number)

        # ── Governance rules
        if bundle.governance_rules:
            parts.append("## Governance Rules")
            for path, content in bundle.governance_rules.items():
                parts.append(f"\n### {path}")
                parts.append(f"```\n{content}\n```")
            parts.append("")

        # ── Changed files (profile-scoped snippets or full content)
        if bundle.files_changed:
            parts.append("## Changed Files")
            for path, content in bundle.files_changed.items():
                # Truncate very large files to avoid token explosion
                display = content[:50000] if len(content) > 50000 else content
                suffix = "\n... (truncated)" if len(content) > 50000 else ""
                parts.append(f"\n### {path}")
                parts.append(f"```\n{display}{suffix}\n```")
            parts.append("")

        # ── Unified diff
        if bundle.unified_diffs:
            parts.append("## Unified Diff")
            for path, diff in bundle.unified_diffs.items():
                parts.append(f"\n### {path}")
                parts.append(f"```diff\n{diff}\n```")
            parts.append("")

        # ── Dependency context (includes import-following contract extractions)
        if bundle.dependency_context:
            parts.append("## Dependency & Import Context (read-only, unchanged files)")
            parts.append(
                "These are contract definitions from files imported by the changed code. "
                "They are NOT part of the submitted change — they provide read-only context "
                "for validating that the changed code correctly uses the contracts it imports "
                "(storage layouts, key builders, schema shapes, constants). "
                "Flag any mismatch between the changed code and these contracts."
            )
            for path, content in bundle.dependency_context.items():
                # Keep dependency context excerpts brief
                display = content[:10000] if len(content) > 10000 else content
                parts.append(f"\n### {path}")
                parts.append(f"```\n{display}\n```")
            parts.append("")

        # ── Closing notes
        parts.append("## Review Guidance")
        parts.append(
            "- Use blocking only when all four conditions hold: grounded in evidence, "
            "relevant to the current task, materially affects correctness/safety/"
            "governance, and worth another review cycle."
        )
        parts.append(
            "- Cite governance rule references in `rule_refs` when applicable."
        )
        parts.append(
            "- Prefer no finding over a weak finding. Prefer task completion over "
            "speculative perfection."
        )
        parts.append(
            "- `recommended_autofix` must be low-risk, local, and behavior-preserving. "
            "Include a precise unified diff in `autofix_diff`."
        )
        parts.append(
            "- Use `recommended_review` for ambiguous, medium-value, or architectural "
            "concerns that need human judgment but should not block."
        )
        parts.append('- If the code is clean, return: {"verdict": "pass", "findings": []}')

        return "\n".join(parts)

    def _parse_response(self, raw: str) -> tuple[dict[str, Any], bool]:
        """Parse the JSON response. Returns (parsed_dict, success_flag)."""
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or "verdict" not in parsed:
                logger.error("Reviewer response missing 'verdict' key: %s", raw[:500])
                return {}, False
            return parsed, True
        except json.JSONDecodeError:
            logger.error("Failed to parse reviewer response as JSON: %s", raw[:500])
            return {}, False

    def _to_finding(self, data: dict[str, Any]) -> Finding:
        """Convert a raw finding dict to a Finding domain object."""
        tier_map = {
            "blocking": FindingTier.BLOCKING,
            "recommended_autofix": FindingTier.RECOMMENDED_AUTOFIX,
            "recommended_review": FindingTier.RECOMMENDED_REVIEW,
            "advisory": FindingTier.ADVISORY,
        }
        severity_map = {
            "high": FindingSeverity.HIGH,
            "medium": FindingSeverity.MEDIUM,
            "low": FindingSeverity.LOW,
        }
        return Finding(
            tier=tier_map.get(data.get("tier", ""), FindingTier.ADVISORY),
            category=data.get("category", "correctness"),
            file_path=data.get("file_path", "unknown"),
            line_start=data.get("line_start"),
            line_end=data.get("line_end"),
            message=data.get("message", ""),
            suggestion=data.get("suggestion"),
            autofix_safe=data.get("autofix_safe", False),
            severity=severity_map.get(data.get("severity", ""), FindingSeverity.MEDIUM),
            confidence=max(0.0, min(1.0, data.get("confidence", 0.8))),
            rule_refs=data.get("rule_refs", []),
            why_now=data.get("why_now"),
            autofix_diff=data.get("autofix_diff"),
            status=FindingStatus.OPEN,
        )

    def _to_verdict(self, raw: str, findings: list[Finding]) -> Verdict:
        """Determine verdict from raw string and actual findings.

        Trust-but-verify: the model's verdict is a hint, but we
        derive the actual verdict from the findings themselves.
        """
        has_blocking = any(f.tier == FindingTier.BLOCKING for f in findings)
        has_autofix = any(f.tier == FindingTier.RECOMMENDED_AUTOFIX for f in findings)

        if has_blocking or has_autofix:
            return Verdict.BLOCKING

        has_review = any(f.tier == FindingTier.RECOMMENDED_REVIEW for f in findings)
        if has_review:
            # If model explicitly said requires_human_review and there are
            # review-tier findings, honor the escalation
            if raw == "requires_human_review":
                return Verdict.REQUIRES_HUMAN_REVIEW
            return Verdict.PASS_WITH_ADVISORIES

        has_advisories = any(f.tier == FindingTier.ADVISORY for f in findings)
        if has_advisories:
            return Verdict.PASS_WITH_ADVISORIES

        return Verdict.PASS

    def _estimate_cost(self, tokens_in: int, tokens_out: int) -> float | None:
        """Estimate cost only when explicit model rates are configured."""
        if self.input_cost_per_1m is None or self.output_cost_per_1m is None:
            return None
        return (
            (tokens_in * self.input_cost_per_1m / 1_000_000)
            + (tokens_out * self.output_cost_per_1m / 1_000_000)
        )
