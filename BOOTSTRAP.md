# Pattern Pilot — Bootstrap Guide (v1 First Session)

This document tells Claude **what to build first** and **in what order** when starting the Pattern Pilot project from scratch. Read `CLAUDE.md` for full architecture context before proceeding.

---

## Prerequisites

Before starting, confirm:

1. Docker Desktop is running
2. Python 3.11+ is available on the host
3. An OpenAI API key is ready (user will add to `.env`)
4. The Pattern Pilot Architecture v1 document has been reviewed

---

## Phase 1: Repository Scaffold

**Goal:** Git repo, dependency management, Docker infrastructure.

### Steps

1. Initialize git repo in this directory (`git init`)
2. Create `pyproject.toml` with project metadata and dependencies:
   - fastapi, uvicorn[standard], sqlalchemy[asyncio], asyncpg, alembic
   - pydantic>=2.5, pydantic-settings
   - openai, httpx, typer, rich
   - mcp (Python MCP SDK)
   - Dev deps: pytest, pytest-asyncio, ruff, mypy
3. Create `.env.example`:
   ```
   OPENAI_API_KEY=sk-...
   DATABASE_URL=postgresql+asyncpg://pp:pp_dev@localhost:5437/pattern_pilot
   PP_MAX_ROUNDS=3
   PP_DEFAULT_REVIEW_PROFILE=standard
   PP_LOG_LEVEL=INFO
   ```
4. Create `.gitignore` (Python defaults + .env + __pycache__ + .mypy_cache)
5. Create `docker-compose.yml` with:
   - `pattern-pilot-db`: postgres:16, port 5437:5432, DB=pattern_pilot, user=pp, password=pp_dev
   - `pattern-pilot-api`: builds from Dockerfile, port 8100:8100, depends on db
   - Volume: `pp_pgdata`
6. Create `Dockerfile` (Python 3.11-slim, install deps, run uvicorn on 8100)
7. Create the package directory structure:
   ```
   pattern_pilot/
   ├── __init__.py
   ├── core/
   ├── checks/
   ├── context/
   ├── connectors/
   ├── scanner/
   ├── governance/
   ├── memory/
   ├── policies/
   ├── reporting/
   ├── db/
   │   └── migrations/
   ├── api/
   │   └── routes/
   └── mcp_server.py
   ```
   Each subdirectory gets an `__init__.py`.
8. `docker compose up -d pattern-pilot-db` — start Postgres only
9. Verify DB connection: `psql -h localhost -p 5437 -U pp -d pattern_pilot`
10. Commit: "scaffold: repo init, Docker, package structure"

---

## Phase 2: Core Contracts & Database

**Goal:** Define the data model — this is the foundation everything else depends on.

### Steps

1. **`pattern_pilot/core/contracts.py`** — Pydantic models and enums:
   - `FindingTier` enum: BLOCKING, RECOMMENDED_AUTOFIX, RECOMMENDED_REVIEW, ADVISORY
   - `Verdict` enum: BLOCKING, REQUIRES_HUMAN_REVIEW, PASS_WITH_ADVISORIES, PASS
   - `ReviewStatus` enum: PENDING, RUNNING, PASSED, PASSED_WITH_ADVISORIES, ESCALATED, FAILED
   - `ReviewProfile` enum: QUICK, STANDARD, DEEP
   - `HumanOverride` enum: ACCEPTED, WAIVED, DEFERRED, FALSE_POSITIVE
   - Pydantic models: `Finding`, `ReviewRoundResult`, `ReviewRunResult`, `SubmitRequest`, `SubmitResponse`

2. **`pattern_pilot/core/config.py`** — Settings via pydantic-settings:
   - `DATABASE_URL`, `OPENAI_API_KEY`, `PP_MAX_ROUNDS`, `PP_DEFAULT_REVIEW_PROFILE`, `PP_LOG_LEVEL`

3. **`pattern_pilot/db/session.py`** — Async engine + session factory:
   - `create_async_engine` with the DATABASE_URL
   - `async_sessionmaker` for dependency injection
   - `get_session()` async generator

4. **`pattern_pilot/db/models.py`** — SQLAlchemy 2.0 models for all 7 tables:
   - `Project` — id, name, repo_path, connector_type, connector_config (JSONB), governance_paths (JSONB), completion_gates (JSONB), created_at, updated_at
   - `ReviewSubmission` — id, run_id (FK), submission_number, diff_hash, files_changed (JSONB), deterministic_results (JSONB), deterministic_passed, progressed_to_llm, created_at
   - `ReviewRun` — id, project_id (FK), task_ref, status, verdict, review_profile, governance_snapshot (JSONB), prompt_version, diff_hash, task_title_snapshot, task_status_snapshot, project_context_snapshot (JSONB), connector_type, connector_capabilities (JSONB), total_submissions, total_rounds, started_at, completed_at, created_at
   - `ReviewRound` — id, run_id (FK), round_number, request_payload (JSONB), response_payload (JSONB), verdict, model_used, tokens_in, tokens_out, cost_usd, duration_ms, created_at
   - `Finding` — id, round_id (FK), run_id (FK), tier, category, file_path, line_start, line_end, message, suggestion, autofix_safe, status, human_override, created_at
   - `Advisory` — id, project_id (FK), task_ref, finding_id (FK), message, category, status, created_at
   - `EventLog` — id, project_id (FK), run_id (FK), event_type, payload (JSONB), created_at

5. **Alembic setup:**
   - `alembic init pattern_pilot/db/migrations`
   - Configure `env.py` to use async engine and import models
   - `alembic revision --autogenerate -m "initial_schema"`
   - `alembic upgrade head`
   - Verify all 7 tables exist in Postgres

6. Commit: "feat: core contracts, DB models, initial migration"

---

## Phase 3: Connectors & Governance

**Goal:** Read from target projects without coupling to them.

### Steps

1. **`pattern_pilot/connectors/base.py`** — Abstract connector:
   - Capability enum: GOVERNANCE_READ, GIT_CONTEXT_READ, TASK_READ, DEPENDENCY_READ, TEST_READ
   - Abstract methods: `get_capabilities()`, `read_governance()`, `read_changed_files()`, `read_file()`, `read_task()` (optional), `read_dependencies()` (optional)

2. **`pattern_pilot/connectors/filesystem.py`** — Generic filesystem connector:
   - Reads files from any local directory
   - Uses git to detect changed files
   - Capabilities: GOVERNANCE_READ, GIT_CONTEXT_READ

3. **`pattern_pilot/connectors/story_engine.py`** — Story Engine-specific:
   - Extends filesystem connector
   - Knows Story Engine governance paths (`backend/governance/`, etc.)
   - Optionally calls Story Engine API (localhost:8000) for task info
   - Capabilities: all five

4. **`pattern_pilot/governance/loader.py`** — Governance file reader:
   - Loads governance files via connector
   - Computes content hashes per file
   - Returns versioned governance snapshot

5. **`pattern_pilot/scanner/project_scanner.py`** — Auto-discovery:
   - Detects tech stack (Python/JS/etc.), key directories, config files
   - Used during project onboarding

6. Commit: "feat: connectors, governance loader, project scanner"

---

## Phase 4: Deterministic Checks & Context Builder

**Goal:** The pre-LLM validation layer and diff-scoped bundling.

### Steps

1. **`pattern_pilot/checks/runner.py`** — Deterministic check runner:
   - Runs lint (ruff), typecheck (mypy), tests (pytest) against target project
   - Returns structured results per check
   - Fail here = immediate return, no LLM round consumed

2. **`pattern_pilot/context/bundle_builder.py`** — Diff-scoped context:
   - Takes changed files list from connector
   - Pulls file contents, dependency graph (if available), governance rules
   - Builds a context bundle sized for the review profile (quick/standard/deep)
   - Includes test results from deterministic checks

3. Commit: "feat: deterministic checks, context bundle builder"

---

## Phase 5: OpenAI Reviewer & Orchestrator

**Goal:** The brain — structured LLM review and the QC feedback loop.

### Steps

1. **`pattern_pilot/core/reviewer.py`** — OpenAI API client:
   - Sends context bundle with structured prompt
   - Expects structured response: list of findings with tier, category, file, line, message, suggestion, autofix_safe
   - Includes prompt version tracking
   - Parses response into `Finding` objects

2. **`pattern_pilot/core/orchestrator.py`** — The QC loop:
   - `submit_for_review(project_name, task_ref, files_changed)` entry point
   - Step 1: Load project config + connector
   - Step 2: Run deterministic checks → if fail, return immediately (log submission)
   - Step 3: Build context bundle
   - Step 4: Send to reviewer → get findings
   - Step 5: Evaluate verdict based on finding tiers
   - Step 6: If BLOCKING or recommended_autofix → return findings for Claude to fix → await resubmit
   - Step 7: If PASS / PASS_WITH_ADVISORIES → log, exit
   - Step 8: If round > max_rounds with unresolved Tier 1 → REQUIRES_HUMAN_REVIEW
   - All state changes written to DB via memory store

3. **`pattern_pilot/memory/store.py`** — DB write layer:
   - Creates/updates review_runs, review_rounds, findings, advisories, event_log
   - All writes go through here (single responsibility)

4. **`pattern_pilot/policies/gates.py`** — Completion gate evaluation:
   - Project-specific "done" criteria
   - Evaluates whether a review run meets the project's completion gates

5. **`pattern_pilot/reporting/writer.py`** — Report generation:
   - JSON report (machine-readable)
   - Markdown report (human-readable)
   - Summary for MCP response

6. Commit: "feat: OpenAI reviewer, orchestrator loop, memory store, reporting"

---

## Phase 6: FastAPI API

**Goal:** The control plane REST API.

### Steps

1. **`pattern_pilot/api/server.py`** — FastAPI app factory:
   - Lifespan handler for DB engine
   - CORS, error handlers, health check

2. **`pattern_pilot/api/routes/projects.py`** — Project management:
   - `POST /projects` — onboard a new project (scan + configure)
   - `GET /projects` — list all projects
   - `GET /projects/{id}` — project detail
   - `PUT /projects/{id}` — update config
   - `POST /projects/{id}/rescan` — re-scan project

3. **`pattern_pilot/api/routes/reviews.py`** — Review history:
   - `GET /projects/{id}/reviews` — list review runs
   - `GET /reviews/{id}` — review detail with rounds and findings
   - `GET /reviews/{id}/rounds` — rounds for a run
   - `GET /reviews/{id}/findings` — findings for a run

4. **`pattern_pilot/api/routes/metrics.py`** — Analytics:
   - `GET /projects/{id}/metrics` — pass rates, avg rounds, cost trends
   - `GET /metrics/summary` — cross-project summary

5. **`pattern_pilot/api/routes/advisories.py`** — Advisory management:
   - `GET /projects/{id}/advisories` — browse advisories
   - `PUT /advisories/{id}` — dismiss, defer, acknowledge

6. Commit: "feat: FastAPI API with all route groups"

---

## Phase 7: MCP Server

**Goal:** The Claude Desktop integration point.

### Steps

1. **`pattern_pilot/mcp_server.py`** — MCP entry point:
   - Tool: `submit_for_review(project_name, task_ref, files_changed, review_profile?)` — triggers the orchestrator
   - Tool: `get_review_status(run_id)` — check status of a review
   - Tool: `list_projects()` — show onboarded projects
   - Tool: `get_advisories(project_name)` — recent advisories for context
   - Runs on HOST (not in Docker), connects to API at localhost:8100

2. Add MCP server config to Claude Desktop's `claude_desktop_config.json`
3. Test end-to-end: submit a review from Claude Desktop → deterministic checks → OpenAI review → findings returned

4. Commit: "feat: MCP server for Claude Desktop integration"

---

## Phase 8: Tests

**Goal:** Test coverage for all core modules.

### Steps

1. `tests/test_contracts.py` — enum values, pydantic validation
2. `tests/test_orchestrator.py` — mock reviewer, test loop logic
3. `tests/test_reviewer.py` — mock OpenAI responses, structured parsing
4. `tests/test_checks.py` — mock subprocess calls for lint/typecheck/tests
5. `tests/test_bundle_builder.py` — context assembly with mock connector
6. `tests/test_connectors.py` — filesystem connector against temp directory
7. `tests/conftest.py` — shared fixtures, async test setup

8. Commit: "test: core test suite"

---

## Phase 9: Onboard Story Engine

**Goal:** First real target project.

### Steps

1. Start full Docker stack: `docker compose up -d`
2. Via API or MCP, onboard Story Engine:
   - project_name: "story-engine"
   - repo_path: `/Users/pavanktalari/Projects/AmiTara/story-engine/`
   - connector_type: "story_engine"
   - governance_paths: `["backend/governance/", "backend/app/config/governance_policy.py", "backend/app/config/qc_registry.py"]`
3. Verify governance files are loaded and hashed
4. Do a test review on a known Story Engine change
5. Verify findings come back with correct tiers and verdicts

6. Commit: "feat: Story Engine onboarding config"

---

## Build Order Summary

| Phase | What | Depends On |
|-------|------|------------|
| 1 | Scaffold (repo, Docker, dirs) | Nothing |
| 2 | Contracts + DB models + migration | Phase 1 |
| 3 | Connectors + governance loader | Phase 2 (contracts) |
| 4 | Deterministic checks + context builder | Phase 3 (connectors) |
| 5 | Reviewer + orchestrator + memory store | Phase 2-4 (all) |
| 6 | FastAPI API | Phase 2 (DB), Phase 5 (orchestrator) |
| 7 | MCP server | Phase 5 (orchestrator), Phase 6 (API) |
| 8 | Tests | Phase 2-7 (all modules) |
| 9 | Onboard Story Engine | Phase 1-7 (full system) |

---

## First Command After Opening This Project

```bash
# Verify Docker is running, then:
docker compose up -d pattern-pilot-db
```

Then start Phase 1 scaffold, work through each phase in order. Each phase should end with a commit.
