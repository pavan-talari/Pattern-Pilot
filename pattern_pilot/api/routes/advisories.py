"""Advisory management routes — browse, dismiss, defer."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pattern_pilot.core.contracts import AdvisoryStatus
from pattern_pilot.db import models
from pattern_pilot.db.session import get_session

router = APIRouter()


class AdvisoryResponse(BaseModel):
    id: str
    project_id: str
    task_ref: str
    finding_id: str | None
    message: str
    category: str
    status: str

    model_config = {"from_attributes": True}


class AdvisoryUpdate(BaseModel):
    status: str  # dismissed, deferred, acknowledged


@router.get(
    "/projects/{project_id}/advisories", response_model=list[AdvisoryResponse]
)
async def list_advisories(
    project_id: str,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Browse advisories for a project, optionally filtered by status."""
    query = select(models.Advisory).where(
        models.Advisory.project_id == project_id
    )
    if status:
        query = query.where(models.Advisory.status == status)
    query = query.order_by(models.Advisory.created_at.desc())

    result = await session.execute(query)
    return result.scalars().all()


@router.put("/advisories/{advisory_id}", response_model=AdvisoryResponse)
async def update_advisory(
    advisory_id: str,
    body: AdvisoryUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Dismiss, defer, or acknowledge an advisory."""
    advisory = await session.get(models.Advisory, advisory_id)
    if not advisory:
        raise HTTPException(404, "Advisory not found")

    # Validate status
    valid = {s.value for s in AdvisoryStatus}
    if body.status not in valid:
        raise HTTPException(400, f"Invalid status. Must be one of: {valid}")

    advisory.status = body.status
    await session.flush()
    return advisory
