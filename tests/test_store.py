"""Tests for DB write coordination in MemoryStore."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pattern_pilot.core.contracts import (
    Finding,
    FindingTier,
    ReviewRoundResult,
    Verdict,
)
from pattern_pilot.db import models
from pattern_pilot.memory.store import MemoryStore


class FakeSession:
    """Small async session test double for store methods that only add/flush."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            obj.id = str(uuid.uuid4())
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _run() -> models.ReviewRun:
    return models.ReviewRun(
        id="run-1",
        project_id="project-1",
        task_ref="TASK-1",
        total_rounds=0,
    )


def _round_with_advisory() -> ReviewRoundResult:
    return ReviewRoundResult(
        round_number=1,
        verdict=Verdict.PASS_WITH_ADVISORIES,
        findings=[
            Finding(
                tier=FindingTier.ADVISORY,
                category="docs",
                file_path="main.py",
                message="Keep an eye on this.",
            ),
            Finding(
                tier=FindingTier.RECOMMENDED_REVIEW,
                category="architecture",
                file_path="main.py",
                message="Human should review later.",
            ),
        ],
        model_used="gpt-5.4",
    )


@pytest.mark.asyncio
async def test_record_round_links_advisory_to_persisted_finding() -> None:
    session = FakeSession()
    store = MemoryStore(session)  # type: ignore[arg-type]

    await store.record_round(_run(), _round_with_advisory(), create_advisories=True)

    findings = [obj for obj in session.added if isinstance(obj, models.Finding)]
    advisories = [obj for obj in session.added if isinstance(obj, models.Advisory)]

    assert len(findings) == 2
    assert len(advisories) == 1
    assert advisories[0].finding_id == findings[0].id


@pytest.mark.asyncio
async def test_record_round_does_not_create_advisory_unless_requested() -> None:
    session = FakeSession()
    store = MemoryStore(session)  # type: ignore[arg-type]

    await store.record_round(_run(), _round_with_advisory(), create_advisories=False)

    advisories = [obj for obj in session.added if isinstance(obj, models.Advisory)]

    assert advisories == []
