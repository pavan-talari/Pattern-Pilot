"""Project scanner — auto-discovers tech stack, key files, and directories.

Used during project onboarding. Pattern Pilot does NOT hardcode knowledge
about any target project — this scanner discovers everything at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# File → tech-stack signal mapping
_STACK_SIGNALS: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "package.json": "javascript",
    "tsconfig.json": "typescript",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
    "mix.exs": "elixir",
    "composer.json": "php",
}

_FRAMEWORK_SIGNALS: dict[str, str] = {
    "manage.py": "django",
    "app.py": "flask",
    "next.config.js": "nextjs",
    "next.config.ts": "nextjs",
    "nuxt.config.js": "nuxt",
    "angular.json": "angular",
    "svelte.config.js": "svelte",
    "astro.config.mjs": "astro",
    "vite.config.ts": "vite",
    "webpack.config.js": "webpack",
}

_TOOL_SIGNALS: dict[str, str] = {
    "Dockerfile": "docker",
    "docker-compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
    ".github": "github-actions",
    ".gitlab-ci.yml": "gitlab-ci",
    "Makefile": "make",
    "alembic.ini": "alembic",
    "pytest.ini": "pytest",
    "ruff.toml": "ruff",
    ".eslintrc.json": "eslint",
    ".prettierrc": "prettier",
}


class ProjectScanError(RuntimeError):
    """Expected scanner failure caused by unsupported project metadata."""


@dataclass
class ScanResult:
    """Result of scanning a project directory."""

    repo_path: str
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    key_directories: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    has_git: bool = False
    governance_candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "languages": self.languages,
            "frameworks": self.frameworks,
            "tools": self.tools,
            "key_directories": self.key_directories,
            "config_files": self.config_files,
            "has_git": self.has_git,
            "governance_candidates": self.governance_candidates,
        }


class ProjectScanner:
    """Scans a project directory to discover its tech stack and structure.

    Used during onboarding to auto-populate project metadata.
    """

    def __init__(self, repo_path: str) -> None:
        self.repo_path = Path(repo_path)

    def scan(self) -> ScanResult:
        """Run a full scan and return results."""
        if not self.repo_path.is_dir():
            raise FileNotFoundError(f"Project path not found: {self.repo_path}")

        result = ScanResult(repo_path=str(self.repo_path))

        # Check for git
        result.has_git = (self.repo_path / ".git").is_dir()

        # Walk top-level and first-level children for signals
        top_entries = set(os.listdir(self.repo_path))

        # Detect languages
        for filename, lang in _STACK_SIGNALS.items():
            if filename in top_entries and lang not in result.languages:
                result.languages.append(lang)
                result.config_files.append(filename)

        # Detect frameworks
        for filename, framework in _FRAMEWORK_SIGNALS.items():
            if filename in top_entries and framework not in result.frameworks:
                result.frameworks.append(framework)

        # Detect tools
        for filename, tool in _TOOL_SIGNALS.items():
            if filename in top_entries and tool not in result.tools:
                result.tools.append(tool)
                if filename not in result.config_files:
                    result.config_files.append(filename)

        # Key directories (top-level dirs, excluding hidden/common noise)
        _skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache"}
        for entry in sorted(top_entries):
            if entry.startswith(".") and entry != ".github":
                continue
            if entry in _skip:
                continue
            full = self.repo_path / entry
            if full.is_dir():
                result.key_directories.append(entry)

        # Mixed repos often keep backend/frontend config one level below root.
        # Scan first-level directories for additional language/framework/tool signals.
        for top_dir in result.key_directories:
            try:
                child_entries = set(os.listdir(self.repo_path / top_dir))
            except OSError:
                continue

            for filename, lang in _STACK_SIGNALS.items():
                if filename in child_entries and lang not in result.languages:
                    result.languages.append(lang)
                if filename in child_entries:
                    child_path = f"{top_dir}/{filename}"
                    if child_path not in result.config_files:
                        result.config_files.append(child_path)

            for filename, framework in _FRAMEWORK_SIGNALS.items():
                if filename in child_entries and framework not in result.frameworks:
                    result.frameworks.append(framework)

            for filename, tool in _TOOL_SIGNALS.items():
                if filename in child_entries and tool not in result.tools:
                    result.tools.append(tool)
                if filename in child_entries:
                    child_path = f"{top_dir}/{filename}"
                    if child_path not in result.config_files:
                        result.config_files.append(child_path)

        # Governance candidates — look for dirs/files with common governance names
        _gov_names = {"governance", "rules", "policies", "standards", "guidelines", "docs"}
        for entry in top_entries:
            full = self.repo_path / entry
            if entry.lower() in _gov_names and (full.is_dir() or full.is_file()):
                result.governance_candidates.append(entry)
        # Also check one level deep
        for top_dir in result.key_directories:
            try:
                for child in os.listdir(self.repo_path / top_dir):
                    if child.lower() in _gov_names:
                        candidate = f"{top_dir}/{child}"
                        if candidate not in result.governance_candidates:
                            result.governance_candidates.append(candidate)
            except PermissionError:
                continue

        return result
