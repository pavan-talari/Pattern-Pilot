"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from pathlib import Path

from datetime import timedelta

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pattern_pilot.core.config import get_settings, pp_now
from pattern_pilot.db import models
from pattern_pilot.db.session import engine, AsyncSessionLocal, get_session

logger = logging.getLogger(__name__)


async def _cleanup_stale_runs(max_age_hours: int = 1) -> int:
    """Mark runs stuck in 'running'/'blocked' for longer than max_age_hours as 'abandoned'."""
    # Use naive UTC to match the DB column (TIMESTAMP WITHOUT TIME ZONE)
    cutoff = pp_now() - timedelta(hours=max_age_hours)
    now = pp_now()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(models.ReviewRun)
            .where(
                models.ReviewRun.status.in_(["running", "blocked"]),
                models.ReviewRun.created_at < cutoff,
            )
            .values(
                status="abandoned",
                verdict="abandoned",
                completed_at=now,
            )
        )
        await session.commit()
        rowcount = getattr(cast(Any, result), "rowcount", 0)
        return int(rowcount or 0)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown — manage DB engine lifecycle."""
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.pp_log_level, logging.INFO))
    logger.info("Pattern Pilot API starting (prompt_version=%s)", settings.pp_prompt_version)

    # Auto-clean stale runs on startup
    cleaned = await _cleanup_stale_runs(max_age_hours=1)
    if cleaned:
        logger.info("Cleaned up %d stale running review(s)", cleaned)

    yield
    await engine.dispose()
    logger.info("Pattern Pilot API shut down.")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    from pattern_pilot.api.routes import advisories, config, metrics, projects, reviews

    app = FastAPI(
        title="Pattern Pilot",
        description="Code quality control plane — dual-LLM review loop",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health check
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "pattern-pilot"}

    # Dashboard
    @app.get("/dashboard")
    async def dashboard() -> Any:
        """Serve the Pattern Pilot dashboard."""
        dashboard_path = Path(__file__).resolve().parent.parent.parent / "dashboard.html"
        if dashboard_path.is_file():
            return FileResponse(dashboard_path, media_type="text/html")
        return {"error": "dashboard.html not found"}

    # Event log endpoint (for audit dashboard)
    @app.get("/events")
    async def list_events(
        limit: int = 100, session: AsyncSession = Depends(get_session)
    ) -> list[dict[str, Any]]:
        """Return recent audit events."""
        result = await session.execute(
            select(models.EventLog)
            .order_by(models.EventLog.created_at.desc())
            .limit(limit)
        )
        events = result.scalars().all()
        return [
            {
                "id": e.id,
                "project_id": e.project_id,
                "run_id": e.run_id,
                "event_type": e.event_type,
                "payload": e.payload,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]

    # Cleanup stale runs
    @app.post("/reviews/cleanup")
    async def cleanup_stale_runs(max_age_hours: int = 1) -> dict[str, int]:
        """Mark reviews stuck in 'running' as 'abandoned'."""
        cleaned = await _cleanup_stale_runs(max_age_hours=max_age_hours)
        return {"cleaned": cleaned, "max_age_hours": max_age_hours}

    # Register route groups
    app.include_router(projects.router, prefix="/projects", tags=["Projects"])
    app.include_router(reviews.router, tags=["Reviews"])
    app.include_router(metrics.router, tags=["Metrics"])
    app.include_router(advisories.router, tags=["Advisories"])
    app.include_router(config.router, tags=["Config"])

    return app
