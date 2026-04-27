# Pattern Pilot — Project Instructions

## Identity

Pattern Pilot is a **standalone, project-agnostic code quality control plane**. It orchestrates a review loop where Codex (code writer) and OpenAI (code reviewer) validate code against a target project's own governance rules.

Pattern Pilot is **NOT** part of Story Engine. It is a completely independent system with its own repository, database, and API. Story Engine is merely the first target project it connects to.

## Core Principle

**Dual truth boundaries.** Pattern Pilot owns QC state (review runs, findings, metrics, escalation history, onboarding config). The target project owns workflow state (tasks, dependencies, governance rules, codebase). Pattern Pilot reads from the target project but **never writes to it**.

## Repository Path

```
/Users/pavanktalari/Projects/AmiTara/Pattern-Pilot/
```

## What Pattern Pilot IS

- An automated QC gate that reviews code after Codex writes it
- A control plane with its own PostgreSQL database, FastAPI API, and MCP server
- Project-agnostic — works with Story Engine today, any project tomorrow
- An MCP server inside Codex Desktop — seamless, no context switching

## What Pattern Pilot is NOT

- NOT a task planner or project manager — it does not pick tasks or plan work
- NOT a code editor — it returns findings only, the coding agent applies fixes
- NOT a replacement for the target project's backlog system
- NOT dependent on any specific project — zero Story Engine code inside it

## Architecture (Four Layers)

### Layer 1: MCP Server (Runtime Engine) — runs on HOST
- Receives `submit_for_review` calls from Codex Desktop
- Runs deterministic checks (lint, typecheck, tests)
- Builds diff-scoped context bundle (changed files + deps + governance)
- Sends to OpenAI for structured review
- Returns tiered findings to Codex
- Manages the QC feedback loop (configurable max rounds, default 3)

### Layer 2: Control Plane API (FastAPI) — Docker container, port 8100
- Project onboarding and configuration
- Review history and provenance queries
- Metrics and analytics endpoints
- Advisory management

### Layer 3: PostgreSQL — Docker container, port 5437
- Pattern Pilot's own database (completely separate from any target project DB)
- Tables: projects, review_submissions, review_runs, review_rounds, findings, advisories, event_log
- JSONB for review payloads, governance snapshots
- Append-only event log for full audit trail

### Layer 4: Web UI (v1.5 — not in v1)
- Project registry and onboarding wizard
- Review history with drill-down
- Metrics dashboard
- Escalation inbox

## Tech Stack

- Python 3.11+
- FastAPI + Uvicorn (API)
- SQLAlchemy 2.0 + asyncpg (async PostgreSQL)
- Alembic (migrations)
- Pydantic 2.5+ (schemas and settings)
- OpenAI SDK (reviewer)
- Anthropic SDK (optional — for programmatic Codex calls in future)
- httpx (async HTTP client for connectors)
- Typer + Rich (CLI, if needed)
- Docker + Docker Compose (Postgres + API containers)
- MCP Python SDK (MCP server)

## Codebase Structure

```
Pattern-Pilot/
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── .env.example
│
├── pattern_pilot/
│   ├── __init__.py
│   │
│   ├── core/
│   │   ├── contracts.py          # ReviewRun, ReviewRound, Finding, Verdict enums
│   │   ├── orchestrator.py       # QC loop: submit → check → review → fix → resubmit
│   │   ├── reviewer.py           # OpenAI API client with structured prompts
│   │   └── config.py             # Settings: max rounds, model, timeouts
│   │
│   ├── checks/
│   │   └── runner.py             # Deterministic: lint, typecheck, tests, forbidden patterns
│   │
│   ├── context/
│   │   └── bundle_builder.py     # Diff-scoped context: changed files, deps, governance, tests
│   │
│   ├── connectors/
│   │   ├── base.py               # Abstract interface + capability model
│   │   ├── filesystem.py         # Generic local directory reader
│   │   └── story_engine.py       # Story Engine-specific connector
│   │
│   ├── scanner/
│   │   └── project_scanner.py    # Auto-discovers tech stack, dirs, key files
│   │
│   ├── governance/
│   │   └── loader.py             # Reads governance files from target project, versions, hashes
│   │
│   ├── memory/
│   │   └── store.py              # Writes to DB: runs, rounds, findings, events
│   │
│   ├── policies/
│   │   └── gates.py              # Project-specific completion gate evaluation
│   │
│   ├── reporting/
│   │   └── writer.py             # JSON + Markdown QC reports
│   │
│   ├── db/
│   │   ├── models.py             # SQLAlchemy models for all 7 tables
│   │   ├── session.py            # Async engine + session factory
│   │   └── migrations/           # Alembic
│   │
│   ├── api/
│   │   ├── server.py             # FastAPI app factory
│   │   └── routes/
│   │       ├── projects.py       # CRUD, onboarding, rescan
│   │       ├── reviews.py        # History, detail, rounds, findings
│   │       ├── metrics.py        # Pass rates, costs, trends
│   │       └── advisories.py     # Browse, dismiss, defer
│   │
│   └── mcp_server.py             # MCP entry point for Codex Desktop
│
├── tests/
│   ├── test_orchestrator.py
│   ├── test_reviewer.py
│   ├── test_checks.py
│   ├── test_bundle_builder.py
│   └── test_connectors.py
│
└── ui/                           # v1.5
```

## Feedback Tiers

| Tier | Name | Action | Loop Behavior |
|------|------|--------|--------------|
| Tier 1 | BLOCKING | Codex auto-fixes | Must resolve to exit loop |
| Tier 2a | recommended_autofix | Codex auto-fixes | Must resolve. Low-risk, local only |
| Tier 2b | recommended_review | Surfaced to user | Does NOT block exit. User decides |
| Tier 3 | advisory | Logged only | Never blocks. Stored in PP memory |

## Verdict Model

| Verdict | Meaning |
|---------|---------|
| BLOCKING | Mandatory fixes required. Loop continues. |
| REQUIRES_HUMAN_REVIEW | Escalation only: unresolved ambiguity, conflicting signals, loop exhaustion, unsafe architectural uncertainty |
| PASS_WITH_ADVISORIES | Code acceptable. Tier 2b surfaced, Tier 3 logged. |
| PASS | Clean. No findings. |

## Runtime QC Flow

1. Codex calls `submit_for_review(project_name, task_ref, files_changed)`
2. Deterministic checks run (lint, typecheck, tests). If fail → return immediately (not a QC round)
3. Build diff-scoped context bundle
4. Send to OpenAI for structured review → tiered findings
5. BLOCKING or recommended_autofix → return to Codex for fix → resubmit (round++)
6. PASS / PASS_WITH_ADVISORIES → log, exit
7. round > max_rounds with unresolved Tier 1 → REQUIRES_HUMAN_REVIEW → escalate

## Execution Model

- **v1: Synchronous.** Codex calls submit_for_review, Pattern Pilot blocks until full loop completes, returns verdict.
- **v1.5+: Background workers.** MCP returns run_id immediately, API monitors progress.
- `review_runs.status` supports both: pending → running → passed/passed_with_advisories/escalated/failed

## Review Profiles

| Profile | Scope |
|---------|-------|
| quick | Diff only. Minimal context. Fast, cheap. |
| standard | Diff + deps + governance + test results. Default. |
| deep | Full module context. For architecture changes, 10+ files, repeated failures. |

## Database Tables (7 tables in Pattern Pilot's own Postgres)

1. **projects** — onboarded project metadata, connector config, governance pins, completion gates
2. **review_submissions** — every submit_for_review call including deterministic failures
3. **review_runs** — one per task review lifecycle, with status, verdict, snapshots
4. **review_rounds** — one per LLM review round within a run
5. **findings** — individual findings with tier, category, file, line, autofix_safe, status
6. **advisories** — Tier 3 long-term notes, linked to project/task
7. **event_log** — append-only audit stream for every state transition

## Key Design Rules

1. Pattern Pilot is a QC control plane, NOT a project manager
2. Dual truth boundaries — PP owns QC state, target project owns workflow state
3. Pattern Pilot NEVER edits files in the target project
4. Governance rules belong to the project, not Pattern Pilot
5. Deterministic checks run BEFORE LLM review
6. Reviews are diff-scoped by default, deep review only when justified
7. Every review run is reproducible: governance hashes, prompt version, diff hash
8. Idempotency: same inputs = cached result unless force_review
9. MCP server is the trigger: explicit submit_for_review call, no magic detection
10. Connectors declare capabilities. Graceful degradation when project doesn't expose tasks/deps

## Docker Setup

```yaml
# docker-compose.yml
services:
  pattern-pilot-db:
    image: postgres:16
    ports: ["5437:5432"]
    environment:
      POSTGRES_DB: pattern_pilot
      POSTGRES_USER: pp
      POSTGRES_PASSWORD: pp_dev
    volumes: [pp_pgdata:/var/lib/postgresql/data]

  pattern-pilot-api:
    build: .
    ports: ["8100:8100"]
    depends_on: [pattern-pilot-db]
    environment:
      DATABASE_URL: postgresql+asyncpg://pp:pp_dev@pattern-pilot-db:5432/pattern_pilot
    volumes: [.:/app]

volumes:
  pp_pgdata:
```

MCP server runs on the HOST machine, not in Docker. It connects to pattern-pilot-api at http://localhost:8100.

## Relationship to Story Engine

- Story Engine is the FIRST target project, not the only one
- Pattern Pilot connects to Story Engine via a connector that reads its filesystem and optionally calls its API (localhost:8000)
- Story Engine's governance files live at:
  - `backend/governance/`
  - `backend/app/config/governance_policy.py`
  - `backend/app/config/qc_registry.py`
- Story Engine path: `/Users/pavanktalari/Projects/AmiTara/story-engine/`
- If Pattern Pilot is removed, Story Engine works exactly the same — zero dependency

## v1 Scope

MCP server, deterministic checks, diff-scoped context builder, structured OpenAI reviewer, PostgreSQL database (7 tables), FastAPI API (all endpoints), provenance store, event log, project onboarding, filesystem + Story Engine connectors, Docker Compose. No UI.

## Out of Scope (v1)

Task planning, task selection, autonomous operation, direct code editing, replacing project backlogs, web UI (v1.5), multi-user collaboration, CI/CD integration.
