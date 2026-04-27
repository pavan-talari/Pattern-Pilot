"""Generic filesystem connector — reads from any local project directory."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from pattern_pilot.connectors.base import BaseConnector, ConnectorInfo
from pattern_pilot.core.contracts import ConnectorCapability

# Timeout for individual file reads and git operations (seconds).
# Prevents hanging when the host filesystem is unresponsive (e.g., Mac sleep).
FILE_IO_TIMEOUT = 30
GIT_CMD_TIMEOUT = 30


class FilesystemConnector(BaseConnector):
    """Reads files and git context from a local directory.

    This is the default, project-agnostic connector. It works with any
    git-tracked project — no hardcoded knowledge of the target project.
    """

    async def check_health(self, timeout: float = 10.0) -> tuple[bool, str]:
        """Quick health check — verify the repo path is readable.

        Returns (is_healthy, message). Used to fail fast when the host
        filesystem is unresponsive (e.g., Mac went to sleep).
        """
        try:
            def _probe() -> str:
                p = Path(self.repo_path)
                if not p.exists():
                    return f"Repo path does not exist: {self.repo_path}"
                if not p.is_dir():
                    return f"Repo path is not a directory: {self.repo_path}"
                # Try to list a few entries — this touches the filesystem
                list(p.iterdir())[:3]
                return ""

            error = await asyncio.wait_for(
                asyncio.to_thread(_probe), timeout=timeout
            )
            if error:
                return False, error
            return True, "ok"
        except asyncio.TimeoutError:
            return False, (
                f"Filesystem health check timed out after {timeout}s. "
                "Host may be asleep or filesystem mount is stale."
            )
        except Exception as exc:
            return False, f"Filesystem health check failed: {exc}"

    def get_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            connector_type="filesystem",
            capabilities=[
                ConnectorCapability.GOVERNANCE_READ,
                ConnectorCapability.GIT_CONTEXT_READ,
            ],
            repo_path=self.repo_path,
            config=self.config,
        )

    async def read_file(self, relative_path: str) -> str:
        """Read a single file from the target project.

        Times out after FILE_IO_TIMEOUT seconds to prevent hanging
        when the host filesystem is unresponsive (e.g., Mac sleep).
        """
        full_path = Path(self.repo_path) / relative_path
        if not full_path.is_file():
            raise FileNotFoundError(f"File not found: {full_path}")

        def _read() -> str:
            raw = full_path.read_bytes()
            return raw.decode("utf-8", errors="replace")

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_read), timeout=FILE_IO_TIMEOUT
            )
        except TimeoutError as exc:
            raise TimeoutError(
                f"File read timed out after {FILE_IO_TIMEOUT}s: {full_path}. "
                "Host filesystem may be unresponsive (Mac asleep?)."
            ) from exc

    async def read_changed_files(
        self, base_ref: str = "HEAD~1", head_ref: str = "HEAD"
    ) -> dict[str, str]:
        """Use git diff to find changed files, return {path: content}."""
        result: dict[str, str] = {}
        diff_output = await self._run_git(
            "diff", "--name-only", "--diff-filter=ACMR", base_ref, head_ref
        )
        for line in diff_output.strip().splitlines():
            rel_path = line.strip()
            if not rel_path:
                continue
            try:
                content = await self.read_file(rel_path)
                result[rel_path] = content
            except FileNotFoundError:
                continue  # File was deleted between diff and read
        return result

    async def read_governance(self, governance_paths: list[str]) -> dict[str, str]:
        """Read governance files/dirs. Discovers files if a directory is given."""
        result: dict[str, str] = {}
        for gpath in governance_paths:
            full = Path(self.repo_path) / gpath
            if full.is_file():
                result[gpath] = await self.read_file(gpath)
            elif full.is_dir():
                for child in sorted(full.rglob("*")):
                    if child.is_file() and not child.name.startswith("."):
                        rel = str(child.relative_to(self.repo_path))
                        result[rel] = await self.read_file(rel)
        return result

    async def list_files(self, directory: str = ".", extensions: list[str] | None = None) -> list[str]:
        """List files in a directory, optionally filtered by extension."""
        root = Path(self.repo_path) / directory
        if not root.is_dir():
            return []

        def _scan() -> list[str]:
            files = []
            for child in sorted(root.rglob("*")):
                if not child.is_file() or child.name.startswith("."):
                    continue
                if extensions and child.suffix not in extensions:
                    continue
                files.append(str(child.relative_to(self.repo_path)))
            return files

        return await asyncio.to_thread(_scan)

    async def get_file_diff(
        self,
        relative_path: str,
        diff_base: str = "HEAD",
        diff_scope: str = "unstaged",
    ) -> str | None:
        """Get unified diff for a single file using git."""
        return await self.get_file_diff_with_options(
            relative_path=relative_path,
            diff_base=diff_base,
            diff_scope=diff_scope,
        )

    async def list_changed_files(
        self,
        diff_base: str = "HEAD",
        diff_scope: str = "unstaged",
    ) -> list[str]:
        """List changed files from git diff according to scope."""
        scope = (diff_scope or "unstaged").lower()
        if scope not in {"unstaged", "staged", "all"}:
            scope = "unstaged"

        async def _names(args: list[str]) -> list[str]:
            output = await self._run_git(*args)
            files = [line.strip() for line in output.splitlines() if line.strip()]
            # Keep only files added/copied/modified/renamed
            return files

        base_args = self._unstaged_base_args(diff_base)
        staged_base = self._staged_base_arg(diff_base)
        untracked = await self._list_untracked_files()

        try:
            if scope == "staged":
                staged_args = ["diff", "--staged", "--name-only", "--diff-filter=ACMR"]
                if staged_base:
                    staged_args.append(staged_base)
                staged = await _names(staged_args)
                return sorted(set(staged))

            if scope == "all":
                unstaged = await _names(
                    ["diff", "--name-only", "--diff-filter=ACMR", *base_args]
                )
                staged_args = ["diff", "--staged", "--name-only", "--diff-filter=ACMR"]
                if staged_base:
                    staged_args.append(staged_base)
                staged = await _names(staged_args)
                return sorted(set([*unstaged, *staged, *untracked]))

            files = await _names(
                ["diff", "--name-only", "--diff-filter=ACMR", *base_args]
            )
            return sorted(set([*files, *untracked]))
        except (RuntimeError, FileNotFoundError):
            return []

    async def get_file_diff_with_options(
        self,
        relative_path: str,
        diff_base: str = "HEAD",
        diff_scope: str = "unstaged",
    ) -> str | None:
        """Get unified diff for a file honoring diff base/scope options."""
        scope = (diff_scope or "unstaged").lower()
        if scope not in {"unstaged", "staged", "all"}:
            scope = "unstaged"
        base_args = self._unstaged_base_args(diff_base)
        staged_base = self._staged_base_arg(diff_base)
        try:
            if scope == "staged":
                staged_args = ["diff", "--staged"]
                if staged_base:
                    staged_args.append(staged_base)
                staged_args.extend(["--", relative_path])
                diff = await self._run_git(*staged_args)
                return diff if diff.strip() else None

            if scope == "all":
                unstaged = await self._run_git(
                    "diff", *base_args, "--", relative_path
                )
                staged_args = ["diff", "--staged"]
                if staged_base:
                    staged_args.append(staged_base)
                staged_args.extend(["--", relative_path])
                staged = await self._run_git(*staged_args)
                if unstaged.strip() and staged.strip() and unstaged != staged:
                    return f"{unstaged.rstrip()}\n\n{staged.lstrip()}"
                merged = unstaged if unstaged.strip() else staged
                return merged if merged.strip() else None

            diff = await self._run_git("diff", *base_args, "--", relative_path)
            return diff if diff.strip() else None
        except (RuntimeError, FileNotFoundError):
            return None

    @staticmethod
    def content_hash(content: str) -> str:
        """SHA-256 hash of file content for governance snapshots."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def _run_git(self, *args: str) -> str:
        """Run a git command in the repo directory.

        Times out after GIT_CMD_TIMEOUT seconds to prevent hanging
        when the host filesystem is unresponsive.
        """
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=GIT_CMD_TIMEOUT
            )
        except TimeoutError as exc:
            proc.kill()
            raise TimeoutError(
                f"git {' '.join(args)} timed out after {GIT_CMD_TIMEOUT}s. "
                "Host filesystem may be unresponsive."
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): "
                f"{stderr.decode(errors='replace')}"
            )
        return stdout.decode(errors="replace")

    @staticmethod
    def _unstaged_base_args(diff_base: str) -> list[str]:
        """Build base-ref args for unstaged diff commands.

        Plain `git diff` is true unstaged scope (working tree vs index). For
        non-default bases, keep the explicit base comparison behavior.
        """
        base = (diff_base or "HEAD").strip() or "HEAD"
        if base == "HEAD":
            return []
        return [base]

    @staticmethod
    def _staged_base_arg(diff_base: str) -> str | None:
        """Build base-ref arg for staged diff commands."""
        base = (diff_base or "HEAD").strip() or "HEAD"
        if base == "HEAD":
            return None
        return base

    async def _list_untracked_files(self) -> list[str]:
        """List untracked files so git-diff mode covers brand-new files."""
        try:
            output = await self._run_git(
                "ls-files", "--others", "--exclude-standard"
            )
        except (RuntimeError, FileNotFoundError):
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]
