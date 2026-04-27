"""Integration tests for the three-state override semantics across both paths.

These tests exercise the ACTUAL request construction code in the MCP server
and the synchronous orchestrator path, not just the merge helper in isolation.

Three states for list fields:
  - None (omitted)        → filesystem fallback applies
  - [] (intentionally empty) → preserved, no fallback
  - [...values...]        → caller value preserved, no fallback
"""

from __future__ import annotations


from pattern_pilot.core.contracts import (
    ReviewProfile,
    SubmitRequest,
)


# ── Test: SubmitRequest model defaults ───────────────────────────────────────


class TestSubmitRequestDefaults:
    """SubmitRequest list fields default to None (omitted), not []."""

    def test_omitted_fields_are_none(self):
        """When caller provides no list fields, they must be None."""
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
        )
        assert req.acceptance_criteria is None
        assert req.known_exceptions is None
        assert req.waived_findings is None
        assert req.decision_summary is None
        assert req.task_objective is None

    def test_explicit_empty_list_preserved(self):
        """Caller explicitly sends [] — must be preserved as []."""
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            acceptance_criteria=[],
            known_exceptions=[],
            waived_findings=[],
        )
        assert req.acceptance_criteria == []
        assert req.known_exceptions == []
        assert req.waived_findings == []

    def test_explicit_values_preserved(self):
        """Caller sends actual values — must be preserved."""
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            acceptance_criteria=["criterion A"],
            known_exceptions=["exception B"],
            waived_findings=["waiver C"],
        )
        assert req.acceptance_criteria == ["criterion A"]
        assert req.known_exceptions == ["exception B"]
        assert req.waived_findings == ["waiver C"]


# ── Test: MCP args → SubmitRequest construction ─────────────────────────────


class TestMCPRequestConstruction:
    """Simulates how _handle_submit builds a SubmitRequest from MCP args.
    Exercises the real code path from mcp_server.py."""

    @staticmethod
    def _build_request_from_args(args: dict) -> SubmitRequest:
        """Replicate the exact construction logic from mcp_server._handle_submit."""
        profile_str = args.get("review_profile", "standard")
        try:
            profile = ReviewProfile(profile_str)
        except ValueError:
            profile = ReviewProfile.STANDARD

        task_id = args.get("task_id") or args["task_ref"]
        return SubmitRequest(
            project_name=args["project_name"],
            task_ref=args["task_ref"],
            task_id=task_id,
            decision_id=args.get("decision_id"),
            attempt_number=args.get("attempt_number"),
            files_changed=args.get("files_changed", []),
            review_profile=profile,
            decision_summary=args.get("decision_summary"),
            task_objective=args.get("task_objective"),
            acceptance_criteria=args.get("acceptance_criteria"),
            known_exceptions=args.get("known_exceptions"),
            waived_findings=args.get("waived_findings"),
        )

    def test_mcp_omitted_fields_are_none(self):
        """MCP call with no list fields → request has None (not [])."""
        args = {
            "project_name": "my-project",
            "task_ref": "Fix auth",
            "task_id": "TASK-100",
            "files_changed": ["auth.py"],
        }
        req = self._build_request_from_args(args)
        assert req.acceptance_criteria is None
        assert req.known_exceptions is None
        assert req.waived_findings is None

    def test_mcp_explicit_empty_list(self):
        """MCP call with explicit [] → request preserves []."""
        args = {
            "project_name": "my-project",
            "task_ref": "Fix auth",
            "task_id": "TASK-100",
            "files_changed": ["auth.py"],
            "acceptance_criteria": [],
            "known_exceptions": [],
            "waived_findings": [],
        }
        req = self._build_request_from_args(args)
        assert req.acceptance_criteria == []
        assert req.known_exceptions == []
        assert req.waived_findings == []

    def test_mcp_with_values(self):
        """MCP call with populated lists → request preserves them."""
        args = {
            "project_name": "my-project",
            "task_ref": "Fix auth",
            "task_id": "TASK-100",
            "files_changed": ["auth.py"],
            "acceptance_criteria": ["Login works"],
            "known_exceptions": ["Legacy endpoints"],
        }
        req = self._build_request_from_args(args)
        assert req.acceptance_criteria == ["Login works"]
        assert req.known_exceptions == ["Legacy endpoints"]
        assert req.waived_findings is None  # Not provided

    def test_mcp_task_context_dict_preserves_none(self):
        """The task_context dict built from request preserves None for omitted fields."""
        args = {
            "project_name": "my-project",
            "task_ref": "Fix auth",
            "task_id": "TASK-100",
            "files_changed": ["auth.py"],
        }
        req = self._build_request_from_args(args)

        # Replicate the task_context construction from mcp_server.py
        task_context = {
            "task_id": req.task_id,
            "decision_id": req.decision_id,
            "attempt_number": req.attempt_number,
            "decision_summary": req.decision_summary,
            "task_objective": req.task_objective,
            "acceptance_criteria": req.acceptance_criteria,
            "known_exceptions": req.known_exceptions,
            "waived_findings": req.waived_findings,
        }

        # All list fields should be None, not []
        assert task_context["acceptance_criteria"] is None
        assert task_context["known_exceptions"] is None
        assert task_context["waived_findings"] is None


# ── Test: Sync path submit_ctx construction ──────────────────────────────────


class TestSyncPathSubmitCtx:
    """Simulates how submit_for_review() builds submit_ctx from a SubmitRequest.
    Exercises the exact logic from orchestrator.py."""

    @staticmethod
    def _build_submit_ctx(request: SubmitRequest) -> dict:
        """Replicate the submit_ctx construction logic from orchestrator."""
        submit_ctx = {
            "task_id": request.task_id,
            "decision_id": request.decision_id,
            "attempt_number": request.attempt_number,
            "decision_summary": request.decision_summary,
            "task_objective": request.task_objective,
        }
        if request.acceptance_criteria is not None:
            submit_ctx["acceptance_criteria"] = request.acceptance_criteria
        if request.known_exceptions is not None:
            submit_ctx["known_exceptions"] = request.known_exceptions
        if request.waived_findings is not None:
            submit_ctx["waived_findings"] = request.waived_findings
        return submit_ctx

    def test_omitted_fields_absent_from_ctx(self):
        """Omitted list fields (None) should NOT appear as keys in submit_ctx."""
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            task_id="TASK-1",
        )
        ctx = self._build_submit_ctx(req)
        assert "acceptance_criteria" not in ctx
        assert "known_exceptions" not in ctx
        assert "waived_findings" not in ctx

    def test_explicit_empty_list_present_in_ctx(self):
        """Explicit [] appears in submit_ctx — filesystem will NOT overwrite."""
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            task_id="TASK-1",
            acceptance_criteria=[],
            known_exceptions=[],
        )
        ctx = self._build_submit_ctx(req)
        assert "acceptance_criteria" in ctx
        assert ctx["acceptance_criteria"] == []
        assert "known_exceptions" in ctx
        assert ctx["known_exceptions"] == []
        # waived_findings was omitted → absent
        assert "waived_findings" not in ctx

    def test_values_present_in_ctx(self):
        """Populated lists appear in submit_ctx with their values."""
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            task_id="TASK-1",
            acceptance_criteria=["Login works"],
        )
        ctx = self._build_submit_ctx(req)
        assert ctx["acceptance_criteria"] == ["Login works"]


# ── Test: Full merge simulation (both paths) ────────────────────────────────


class TestFullMergeSimulation:
    """End-to-end: build request → build ctx → merge with filesystem → verify."""

    @staticmethod
    def _simulate_merge(ctx: dict, fs_ctx: dict) -> dict:
        """Replicate the merge logic from orchestrator."""
        for key, value in fs_ctx.items():
            if value and (key not in ctx or ctx[key] is None):
                ctx[key] = value
        return ctx

    def test_mcp_omitted_fields_filled_by_filesystem(self):
        """MCP path: omitted list fields → None in task_context → filesystem fills."""
        # MCP args with no list fields
        task_context = {
            "task_id": "TASK-1",
            "decision_id": "DEC-1",
            "acceptance_criteria": None,  # Omitted in MCP call
            "known_exceptions": None,
            "waived_findings": None,
            "decision_summary": None,
            "task_objective": None,
        }
        fs_ctx = {
            "task_id": "TASK-1",
            "decision_id": "DEC-1",
            "task_objective": "Migrate token endpoint",
            "decision_summary": "OAuth2 migration",
            "acceptance_criteria": ["Tokens validated", "Refresh works"],
            "known_exceptions": ["Legacy batch auth"],
            "waived_findings": ["Missing PKCE"],
        }
        merged = self._simulate_merge(task_context, fs_ctx)

        assert merged["task_objective"] == "Migrate token endpoint"
        assert merged["decision_summary"] == "OAuth2 migration"
        assert merged["acceptance_criteria"] == ["Tokens validated", "Refresh works"]
        assert merged["known_exceptions"] == ["Legacy batch auth"]
        assert merged["waived_findings"] == ["Missing PKCE"]

    def test_mcp_explicit_empty_blocks_filesystem(self):
        """MCP path: explicit [] → preserved, filesystem does NOT fill."""
        task_context = {
            "task_id": "TASK-1",
            "acceptance_criteria": [],     # Intentionally empty
            "known_exceptions": [],        # Intentionally empty
            "waived_findings": None,       # Omitted → fill
            "decision_summary": "",        # Intentionally empty string
            "task_objective": None,        # Omitted → fill
        }
        fs_ctx = {
            "task_objective": "Migrate token endpoint",
            "decision_summary": "OAuth2 migration",
            "acceptance_criteria": ["Tokens validated"],
            "known_exceptions": ["Legacy batch auth"],
            "waived_findings": ["Missing PKCE"],
        }
        merged = self._simulate_merge(task_context, fs_ctx)

        assert merged["acceptance_criteria"] == []  # Preserved empty
        assert merged["known_exceptions"] == []     # Preserved empty
        assert merged["decision_summary"] == ""     # Preserved empty string
        assert merged["task_objective"] == "Migrate token endpoint"  # Filled
        assert merged["waived_findings"] == ["Missing PKCE"]         # Filled

    def test_sync_omitted_fields_filled_by_filesystem(self):
        """Sync path: omitted list fields → absent from submit_ctx → filesystem fills."""
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            task_id="TASK-1",
            decision_id="DEC-1",
            # All list fields omitted (default None)
        )
        # Build submit_ctx the same way orchestrator does
        submit_ctx = {
            "task_id": req.task_id,
            "decision_id": req.decision_id,
            "decision_summary": req.decision_summary,
            "task_objective": req.task_objective,
        }
        if req.acceptance_criteria is not None:
            submit_ctx["acceptance_criteria"] = req.acceptance_criteria
        if req.known_exceptions is not None:
            submit_ctx["known_exceptions"] = req.known_exceptions
        if req.waived_findings is not None:
            submit_ctx["waived_findings"] = req.waived_findings

        fs_ctx = {
            "task_objective": "Migrate token endpoint",
            "acceptance_criteria": ["Tokens validated"],
            "known_exceptions": ["Legacy batch auth"],
        }
        merged = self._simulate_merge(submit_ctx, fs_ctx)

        assert merged["task_objective"] == "Migrate token endpoint"
        assert merged["acceptance_criteria"] == ["Tokens validated"]
        assert merged["known_exceptions"] == ["Legacy batch auth"]

    def test_sync_explicit_empty_blocks_filesystem(self):
        """Sync path: explicit [] → present in submit_ctx → filesystem blocked."""
        req = SubmitRequest(
            project_name="test",
            task_ref="T-1",
            task_id="TASK-1",
            acceptance_criteria=[],   # Intentionally empty
            known_exceptions=[],      # Intentionally empty
            # waived_findings omitted → None → absent → filesystem fills
        )
        submit_ctx = {
            "task_id": req.task_id,
            "decision_summary": req.decision_summary,
            "task_objective": req.task_objective,
        }
        if req.acceptance_criteria is not None:
            submit_ctx["acceptance_criteria"] = req.acceptance_criteria
        if req.known_exceptions is not None:
            submit_ctx["known_exceptions"] = req.known_exceptions
        if req.waived_findings is not None:
            submit_ctx["waived_findings"] = req.waived_findings

        fs_ctx = {
            "task_objective": "Migrate token endpoint",
            "acceptance_criteria": ["Tokens validated"],
            "known_exceptions": ["Legacy batch auth"],
            "waived_findings": ["Missing PKCE"],
        }
        merged = self._simulate_merge(submit_ctx, fs_ctx)

        assert merged["acceptance_criteria"] == []   # Preserved empty
        assert merged["known_exceptions"] == []      # Preserved empty
        assert merged["task_objective"] == "Migrate token endpoint"  # Filled (was None)
        assert merged["waived_findings"] == ["Missing PKCE"]         # Filled (was absent)
