# Context-Based Review Workflow Proposal

## Purpose

This document proposes the next design direction for Pattern Pilot so review runs preserve task continuity, use richer project context, and reduce low-value iteration loops.

The goal is to make Pattern Pilot behave less like a stateless reviewer and more like a context-aware QC control plane that understands:

1. the project it is reviewing
2. the decision/change stream it belongs to
3. the task currently being executed

This proposal is based on the current Pattern Pilot implementation and the observed review history for Story Engine.

## Problem Statement

Pattern Pilot is conceptually designed as a multi-round QC loop, but the current integration with Story Engine is not preserving stable review identity across retries.

As a result:

- retries are frequently submitted as brand-new runs instead of true resubmits
- prior findings are often not carried forward into the next review
- the reviewer restarts analysis from scratch instead of verifying fixes and surfacing the remaining issues in one pass
- repeated medium-value findings can consume multiple agent cycles without materially improving delivery quality
- the system can optimize locally for review detail while performing poorly globally for task completion speed

The core problem is not only reviewer prompt quality. The bigger issue is missing structured context and missing stable task identity across the review lifecycle.

## Current Observations

### Repository Behavior

Pattern Pilot currently supports:

- project onboarding and governance capture
- deterministic checks before LLM review
- diff-scoped context bundles
- review run / round / finding persistence
- a background MCP submission and polling model

However, it currently identifies resubmits by exact `task_ref` matching. If the caller changes the task label on each retry, Pattern Pilot creates a new run instead of continuing the existing one.

### Observed Review History

Review history from the current Pattern Pilot database for Story Engine shows:

- `169` total review runs
- `169` total review submissions
- average `1.00` submissions per run
- average `0.93` rounds per run
- only `2` runs with real multi-round continuity
- `89` runs whose `task_ref` includes the word `round`
- `97` runs marked `abandoned`

This strongly suggests the main churn is not "one run taking many internal rounds." The main churn is "many retries being stored as separate runs."

### Current Integration Limitation

Pattern Pilot is currently using the generic filesystem connector for Story Engine, not a richer Story Engine-specific connector. That means review context is mostly built from:

- changed files
- git diff
- governance files
- deterministic check output
- one-level import-following

This is useful, but it is still thinner than the intended architecture for decision-aware and task-aware review.

## Root Causes

### 1. Review identity is unstable

Resubmit detection depends on exact `project + task_ref` equality.

If Story Engine or the calling agent submits:

- `TASK-653`
- `TASK-653 round 2`
- `TASK-653 round 3`

Pattern Pilot treats those as separate runs.

### 2. Context is not layered explicitly

The current bundle has useful file-level and governance-level data, but it does not yet carry a first-class model for:

- project context
- decision context
- task context
- accepted exceptions / waived risks
- known architectural intent

### 3. Iteration policy is too binary

The system currently relies heavily on the reviewer verdict and does not yet apply a strong operational policy for:

- repeated weak findings
- repeated medium-severity findings
- false-positive suspicion after repeated cycles
- human override after repeated unresolved rounds
- automatic downgrade / defer / waive rules

### 4. Abandonment breaks continuity

Runs can be marked stale or abandoned too aggressively relative to real developer fix cycles. That further breaks task continuity and makes the historical data noisier.

### 5. Deterministic checks degrade too quietly

If a deterministic tool is missing, the current implementation marks the check as passed. That can push avoidable validation work into the LLM review loop and reduce confidence in the review result.

## Design Direction

The next version of the workflow should explicitly operate on three context layers.

### Layer 1: Project Context

Project context is long-lived and should persist across all tasks for a target project.

It should include:

- project id / name
- repo root
- connector type
- governance paths and hashes
- architecture notes
- critical registries and contracts
- deterministic check configuration
- review policy defaults
- known project-specific exceptions

This is the stable foundation for all reviews in that project.

### Layer 2: Decision Context

Decision context groups related tasks under a shared architectural or product change stream.

Examples:

- `DEC-302`
- `DEC-298`
- migration program
- renderer refactor

It should include:

- `decision_id`
- title / summary
- intended architectural direction
- relevant governance excerpts
- related modules / subsystems
- expected tradeoffs
- approved constraints or exceptions

This gives the reviewer continuity at the change-stream level so it understands why a task exists and what larger design it belongs to.

### Layer 3: Task Context

Task context is the immediate work item currently under review.

It should include:

- stable `task_id`
- human-readable `task_ref`
- objective / acceptance criteria
- changed files
- current attempt number
- prior findings still open
- prior findings already fixed
- waivers / overrides
- reviewer notes from prior attempts

This is where actual submission/resubmit flow should happen.

## Proposed Review Identity Model

Pattern Pilot should stop using mutable human-readable task labels as the only run identity.

### New Submission Identity

Each submission should carry:

- `project_name`
- `decision_id`
- `task_id`
- `task_ref`
- `attempt_number`
- `review_family_id` or equivalent stable run key

### Identity Rules

- `project_name` identifies the onboarded target project
- `decision_id` groups tasks under a shared design/change stream
- `task_id` is the stable task identity
- `task_ref` is display text only and may change
- `attempt_number` is metadata only and must never create a new run
- active-run lookup should prefer `project + task_id`
- if needed, use `project + decision_id + task_id`

### Result

This change would let Pattern Pilot maintain a true multi-round lifecycle for one task, instead of splitting each retry into a new run.

## Proposed Context-Aware Submission Contract

### Minimum Required Fields

```json
{
  "project_name": "story-engine",
  "decision_id": "DEC-302",
  "task_id": "TASK-653",
  "task_ref": "Source-aware prompt families",
  "attempt_number": 4,
  "files_changed": [
    "backend/app/services/prompt_adapters/source_prompt_families.py"
  ],
  "review_profile": "standard"
}
```

### Recommended Extended Fields

```json
{
  "project_name": "story-engine",
  "decision_id": "DEC-302",
  "decision_summary": "Source strategy and prompt-family alignment for edit workflows",
  "task_id": "TASK-653",
  "task_ref": "Source-aware prompt families",
  "attempt_number": 4,
  "task_objective": "Align prompt family selection and invariants with governed shot types and hero semantics",
  "acceptance_criteria": [
    "hero family selection honors hero_type",
    "shot-type aliases stay aligned with governed registry",
    "location-family invariants do not over-constrain time-of-day edits"
  ],
  "files_changed": [
    "backend/app/services/prompt_adapters/source_prompt_families.py"
  ],
  "known_exceptions": [],
  "waived_findings": [],
  "review_profile": "standard"
}
```

## Proposed Review Policy

Not every finding should remain mandatory indefinitely.

### Tier Intent

- `blocking`
  Mandatory by default, but must remain grounded and current-task relevant.

- `recommended_autofix`
  Should be fix-once or auto-apply when safe. Should not keep a task alive for many loops.

- `recommended_review`
  Human judgment. Should not block task completion by default.

- `advisory`
  Informational only. Never blocks.

### Severity and Confidence Policy

Pattern Pilot should incorporate severity and confidence into operational decisions, not just display.

Suggested policy:

- high severity + high confidence + governance/contract relevance:
  keep blocking

- medium severity + medium confidence after repeated rounds:
  eligible for downgrade, defer, or human confirmation

- low severity or low confidence after repeated rounds:
  eligible for waive / ignore / advisory conversion

### Iteration Guardrails

Suggested workflow:

1. First review:
   surface all grounded blocking issues and local autofixes

2. Second review:
   prioritize verification of prior findings and only add genuinely new issues introduced by the fix

3. Third review:
   if the remaining issues are medium/low severity, low confidence, or ambiguous, force one of:
   - downgrade
   - waive
   - false-positive marking
   - human review escalation

4. Do not allow repeated weak findings to continue indefinitely as automatic blockers

## Best Practices For Context-Based Workflow

### For the Calling Agent

- always submit a stable `task_id`
- keep `task_ref` human-readable but do not use it as the run key
- include `decision_id` for every task tied to a broader change stream
- pass changed file paths precisely
- pass task objective and acceptance criteria when available
- send retry count as `attempt_number`, not as part of `task_ref`
- pass known accepted tradeoffs and waived findings explicitly

### For Pattern Pilot

- preserve run continuity across retries
- inject prior findings into every true resubmit
- distinguish verification from fresh analysis
- treat repeated low-value findings differently from new high-confidence blockers
- keep project-level and decision-level context cached and reusable
- prefer one useful review pass over incremental drip-finding

### For Review Prompting

- review the diff first
- validate surrounding contracts using imported dependencies and governed registries
- verify prior findings before searching for new ones
- avoid surfacing speculative hardening unless it is clearly current-task relevant
- do not let local style or non-critical hardening dominate task completion

## Current Gaps To Address

### Gap 1: Stable task identity is missing

Priority: `P1`

Impact:

- breaks resubmit detection
- prevents prior-finding verification
- creates artificial run churn

Current code areas:

- `pattern_pilot/core/orchestrator.py`
- `pattern_pilot/mcp_server.py`
- `pattern_pilot/core/contracts.py`
- `pattern_pilot/db/models.py`

Recommended change:

- add first-class `task_id`, `decision_id`, and `attempt_number`
- use `task_id` for active-run lookup
- keep `task_ref` as display metadata only

### Gap 2: Project / decision / task context is not modeled explicitly

Priority: `P1`

Impact:

- reviewer has weak continuity
- architectural intent is implicit instead of explicit
- tasks under the same decision do not share learned context

Current code areas:

- `pattern_pilot/core/contracts.py`
- `pattern_pilot/context/bundle_builder.py`
- `pattern_pilot/core/reviewer.py`
- `pattern_pilot/db/models.py`

Recommended change:

- extend `SubmitRequest` and `ContextBundle` to carry layered context
- persist decision/task snapshots on runs and submissions

### Gap 3: Review policy does not operationalize repeated weak findings

Priority: `P1`

Impact:

- medium/low-value issues can consume too many cycles
- false positives are discovered too late
- human override comes too late in the loop

Current code areas:

- `pattern_pilot/core/reviewer.py`
- `pattern_pilot/core/orchestrator.py`
- `pattern_pilot/core/contracts.py`
- `pattern_pilot/db/models.py`

Recommended change:

- add policy for downgrade / waive / false_positive / escalate after repeated attempts
- incorporate severity and confidence into loop control

### Gap 4: Abandonment policy is too aggressive for real fix cycles

Priority: `P2`

Impact:

- active work gets fragmented
- historical signal is distorted
- blocked runs lose continuity

Current code areas:

- `pattern_pilot/api/server.py`
- `pattern_pilot/mcp_server.py`

Recommended change:

- extend stale thresholds
- distinguish "stuck infrastructure run" from "developer currently fixing"
- do not auto-abandon blocked runs on short timelines

### Gap 5: Story Engine-specific context integration is still thin

Priority: `P2`

Impact:

- fewer cross-file and task/dependency insights
- reviewer cannot use decision/task metadata from Story Engine directly

Current code areas:

- `pattern_pilot/core/orchestrator.py`
- `pattern_pilot/connectors/base.py`
- `pattern_pilot/connectors/filesystem.py`
- future Story Engine connector implementation

Recommended change:

- implement a richer Story Engine connector or metadata adapter
- expose task metadata, decision metadata, and stronger dependency context

### Gap 6: Deterministic check degradation is too permissive

Priority: `P3`

Impact:

- missing tools are treated as pass
- weakens confidence in "checks passed" state

Current code areas:

- `pattern_pilot/checks/runner.py`

Recommended change:

- distinguish `passed`, `failed`, and `skipped`
- let project policy decide whether skipped checks should block review

## Recommended Implementation Order

### Phase 1: Fix review identity and continuity

1. add `task_id`, `decision_id`, `attempt_number`
2. update active-run lookup to use stable identity
3. stop using `round N` inside `task_ref` for resubmit behavior
4. preserve prior findings across retries

This is the highest-value change.

### Phase 2: Add layered context to submissions and bundles

1. extend contracts and DB models
2. add project/decision/task snapshots
3. pass acceptance criteria, known exceptions, waived findings
4. update reviewer prompt to use the layered model explicitly

### Phase 3: Add review policy controls

1. define downgrade / waive / false_positive flow
2. add repeated-finding handling
3. limit repeated medium/low-value blockers
4. improve escalation rules

### Phase 4: Improve target-project integration

1. add Story Engine-aware context adapter / connector
2. surface task and decision metadata automatically
3. deepen dependency and contract context

### Phase 5: Tighten deterministic confidence

1. distinguish skipped checks
2. let project policy govern skipped-check behavior

## Proposed Success Criteria

The new workflow should be considered successful when:

- retries for one task remain inside one run family
- prior findings are visible and verified in resubmits
- review churn drops materially for repeated task families
- medium/low-value repeated findings do not keep tasks alive indefinitely
- project / decision / task context is present in every review bundle
- Pattern Pilot can distinguish real blockers from advisory hardening more consistently

## Summary

The next direction is not merely prompt tuning. The correct next direction is a context-based workflow built around stable identity and layered review context.

Pattern Pilot should move from:

- project + mutable task label + stateless retries

to:

- project context + decision context + task context + stable resubmit identity

That change is the foundation for reducing iterations, improving reviewer usefulness, and aligning QC effort with actual delivery value.
