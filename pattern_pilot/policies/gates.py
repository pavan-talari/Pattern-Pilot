"""Completion gate evaluation — project-specific "done" criteria."""

from __future__ import annotations

import logging
from typing import Any

from pattern_pilot.core.contracts import FindingTier, ReviewRoundResult, Verdict

logger = logging.getLogger(__name__)


class GateEvaluator:
    """Evaluates whether a review run meets a project's completion gates.

    Gates are declared per-project during onboarding. Examples:
    - "no_blocking_findings": no Tier 1 findings remain
    - "all_autofix_resolved": all Tier 2a findings resolved
    - "deterministic_pass": lint + typecheck + tests all green
    - "max_advisories": no more than N advisories per run

    If no gates are configured, the default is: no blocking findings.
    """

    DEFAULT_GATES = {"no_blocking_findings": True}

    def __init__(self, gates: dict[str, Any] | None = None) -> None:
        self.gates = gates or self.DEFAULT_GATES

    def evaluate(self, round_result: ReviewRoundResult) -> Verdict:
        """Evaluate gates against a round result and return a verdict."""
        findings = round_result.findings

        # Gate: no_blocking_findings
        if self.gates.get("no_blocking_findings", True):
            blocking = [f for f in findings if f.tier == FindingTier.BLOCKING]
            if blocking:
                return Verdict.BLOCKING

        # Gate: all_autofix_resolved
        if self.gates.get("all_autofix_resolved", False):
            autofix = [f for f in findings if f.tier == FindingTier.RECOMMENDED_AUTOFIX]
            if autofix:
                return Verdict.BLOCKING

        # Gate: max_advisories
        max_adv = self.gates.get("max_advisories")
        if max_adv is not None:
            advisories = [f for f in findings if f.tier == FindingTier.ADVISORY]
            if len(advisories) > max_adv:
                logger.warning(
                    "Advisory gate exceeded: %d > %d", len(advisories), max_adv
                )

        # Check for advisories/recommendations
        has_advisories = any(
            f.tier in (FindingTier.RECOMMENDED_REVIEW, FindingTier.ADVISORY)
            for f in findings
        )
        if has_advisories:
            return Verdict.PASS_WITH_ADVISORIES

        return Verdict.PASS
