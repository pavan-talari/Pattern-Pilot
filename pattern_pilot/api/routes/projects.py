"""Project management routes — onboarding, CRUD, rescan."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pattern_pilot.core.config import get_settings, pp_now
from pattern_pilot.db import models
from pattern_pilot.db.session import get_session
from pattern_pilot.scanner.project_scanner import ProjectScanError, ProjectScanner, ScanResult

router = APIRouter()
logger = logging.getLogger(__name__)

AVAILABLE_REVIEWER_PROVIDERS = {"openai", "anthropic", "google", "perplexity"}
REASONING_EFFORTS = {"low", "medium", "high"}


# ── Schemas ──────────────────────────────────────────────────────────────────


class ProjectCreate(BaseModel):
    name: str
    repo_path: str
    connector_type: str = "filesystem"
    connector_config: dict[str, Any] = Field(default_factory=dict)
    governance_paths: list[str] = Field(default_factory=list)
    completion_gates: dict[str, Any] = Field(default_factory=dict)
    reviewer_provider: str = "openai"
    reviewer_model: str | None = None
    reviewer_reasoning_effort: str | None = None


class ProjectUpdate(BaseModel):
    connector_config: dict[str, Any] | None = None
    governance_paths: list[str] | None = None
    completion_gates: dict[str, Any] | None = None


class ProjectModelUpdate(BaseModel):
    reviewer_provider: str = "openai"
    reviewer_model: str | None = None
    reviewer_reasoning_effort: str | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    repo_path: str
    connector_type: str
    connector_config: dict[str, Any]
    governance_paths: list[str]
    completion_gates: dict[str, Any]
    tech_stack: dict[str, Any]
    reviewer_provider: str | None
    reviewer_model: str | None
    reviewer_reasoning_effort: str | None
    archived_at: datetime | None

    model_config = {"from_attributes": True}


class ScanResponse(BaseModel):
    languages: list[str]
    frameworks: list[str]
    tools: list[str]
    key_directories: list[str]
    config_files: list[str]
    has_git: bool
    governance_candidates: list[str]


def _scan_project_or_raise(repo_path: str) -> ScanResult:
    scanner = ProjectScanner(repo_path)
    try:
        return scanner.scan()
    except FileNotFoundError as exc:
        raise HTTPException(400, f"Repo path not found: {repo_path}") from exc
    except (OSError, ProjectScanError) as exc:
        logger.exception("Project scan failed for path %s", repo_path)
        raise HTTPException(
            422,
            (
                "Repo path exists but could not be scanned. "
                f"Check permissions or project metadata. Error: {exc}"
            ),
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected project scanner error for path %s", repo_path)
        raise HTTPException(500, "Project scanner failed unexpectedly.") from exc


def _resolve_repo_path_for_scan(repo_path: str) -> str:
    """Translate a repo path into something the API process can scan.

    We preserve the user-entered repo_path in the database, but the API may need
    a container-visible path for onboarding/rescan when the UI submits a host path.
    """
    expanded = Path(repo_path).expanduser()
    if expanded.is_dir():
        return str(expanded)

    projects_root = Path("/projects")
    candidate = projects_root / expanded.name
    if projects_root.is_dir() and candidate.is_dir():
        return str(candidate)

    settings = get_settings()
    resolved = settings.resolve_repo_path(repo_path)
    if resolved != repo_path:
        return resolved

    return repo_path


def _validate_model_update(body: ProjectModelUpdate) -> None:
    if body.reviewer_provider not in AVAILABLE_REVIEWER_PROVIDERS:
        raise HTTPException(
            400,
            f"Reviewer provider is not available yet: {body.reviewer_provider}",
        )
    if (
        body.reviewer_reasoning_effort is not None
        and body.reviewer_reasoning_effort not in REASONING_EFFORTS
    ):
        raise HTTPException(
            400,
            f"Invalid reasoning effort. Must be one of: {sorted(REASONING_EFFORTS)}",
        )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("", response_model=ProjectResponse, status_code=201)
async def onboard_project(
    body: ProjectCreate, session: AsyncSession = Depends(get_session)
) -> Any:
    """Onboard a new project — scans it and stores config."""
    _validate_model_update(
        ProjectModelUpdate(
            reviewer_provider=body.reviewer_provider,
            reviewer_model=body.reviewer_model,
            reviewer_reasoning_effort=body.reviewer_reasoning_effort,
        )
    )

    # Check for duplicate
    existing = await session.execute(
        select(models.Project).where(
            models.Project.name == body.name,
            models.Project.archived_at.is_(None),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Project '{body.name}' already exists")

    # Auto-scan the project
    scan = _scan_project_or_raise(_resolve_repo_path_for_scan(body.repo_path))

    # Use scanned governance candidates if none provided
    gov_paths = body.governance_paths or scan.governance_candidates

    project = models.Project(
        name=body.name,
        repo_path=body.repo_path,
        connector_type=body.connector_type,
        connector_config=body.connector_config,
        governance_paths=gov_paths,
        completion_gates=body.completion_gates,
        tech_stack=scan.to_dict(),
        reviewer_provider=body.reviewer_provider,
        reviewer_model=body.reviewer_model,
        reviewer_reasoning_effort=body.reviewer_reasoning_effort,
    )
    session.add(project)
    await session.flush()
    return project


@router.patch("/{project_id}/model", response_model=ProjectResponse)
async def update_project_model(
    project_id: str,
    body: ProjectModelUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Update the reviewer model used for future review runs."""
    _validate_model_update(body)
    project = await session.get(models.Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    project.reviewer_provider = body.reviewer_provider
    project.reviewer_model = body.reviewer_model
    project.reviewer_reasoning_effort = body.reviewer_reasoning_effort
    await session.flush()
    return project


@router.get("", response_model=list[ProjectResponse])
async def list_projects(session: AsyncSession = Depends(get_session)) -> Any:
    """List all onboarded projects."""
    result = await session.execute(
        select(models.Project)
        .where(models.Project.archived_at.is_(None))
        .order_by(models.Project.name)
    )
    return result.scalars().all()


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """Get a project by ID."""
    project = await session.get(models.Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Update project configuration."""
    project = await session.get(models.Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if body.connector_config is not None:
        project.connector_config = body.connector_config
    if body.governance_paths is not None:
        project.governance_paths = body.governance_paths
    if body.completion_gates is not None:
        project.completion_gates = body.completion_gates
    await session.flush()
    return project


@router.delete(
    "/{project_id}",
    status_code=200,
    summary="Archive project registration (review history is preserved)",
)
async def delete_project(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    """Archive a project registration while preserving its review history."""
    project = await session.get(models.Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    project.archived_at = pp_now()
    await session.flush()
    return {
        "message": (
            "Project registration archived. Existing review history, findings, "
            "advisories, and metrics are preserved in the backend."
        )
    }


@router.post("/{project_id}/rescan", response_model=ScanResponse)
async def rescan_project(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """Re-scan a project to refresh tech stack and governance candidates."""
    project = await session.get(models.Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    scan = _scan_project_or_raise(_resolve_repo_path_for_scan(project.repo_path))
    project.tech_stack = scan.to_dict()
    await session.flush()
    return scan
