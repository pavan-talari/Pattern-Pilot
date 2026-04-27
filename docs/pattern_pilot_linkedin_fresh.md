# Pattern Pilot LinkedIn Narrative (Fresh Draft)

## Positioning Statement
Pattern Pilot is a standalone, model-agnostic quality control plane for AI-assisted software delivery.

It separates:
- Code writing
- Deterministic validation
- Independent AI review
- Audit/compliance evidence

## One-Line Summary
I use MCP + Pattern Pilot to let one AI model write code and a different AI model review the same repo with governance context, deterministic checks, and an auditable decision trail.

## IMO Workflow
## Input
- Project onboarding metadata
- Changed files
- Task ID and Decision ID
- Governance files

## Mechanism
- `submit_for_review` from MCP
- Deterministic checks first (`lint`, `mypy`, `pytest`)
- Context bundle build (diff + governance + task/decision context + prior findings)
- Structured reviewer output (tiered findings)

## Output
- PASS
- PASS_WITH_ADVISORIES
- BLOCKING with actionable fix suggestions
- REQUIRES_HUMAN_REVIEW (escalation)
- Stored run history and event logs

## Why This Matters
- Independent reviewer model lowers single-model blind spots
- Deterministic gate reduces wasted LLM review cycles
- Tiered findings improve fix prioritization
- Persistent run/round/finding logs improve compliance and audit readiness
- Stable task identity improves resubmit continuity

## Tier Model (Simple View)
- `blocking`: must fix before closure
- `recommended_autofix`: safe, local fix guidance
- `recommended_review`: human decision needed, non-blocking
- `advisory`: informative, logged, non-blocking

## LinkedIn Post Draft
I built a practical AI quality-control workflow called Pattern Pilot.

Instead of using one model to both write and judge code, I split responsibilities:
- A coding agent implements changes
- Pattern Pilot runs deterministic checks first (lint, typecheck, tests)
- A separate reviewer model evaluates the same repository change with governance and task context

What improved:
- Better quality convergence with fewer random loops
- Clear separation of blocking vs advisory findings
- Stronger compliance posture through persisted run, round, finding, and event history

For me, this is less about "AI coding faster" and more about "AI coding with accountable engineering controls."

## Carousel Outline (8 Slides)
1. Problem: AI code review loops are noisy without context
2. Pattern: Separate writer and reviewer models
3. Trigger: MCP submits review run
4. Gate: Deterministic checks before LLM
5. Context: Governance + decision + task + diff
6. Output: Tiered findings and fix loop
7. Closure: Pass, pass-with-advisories, or escalate
8. Impact: Better quality, compliance, and auditability
