"""Application settings via pydantic-settings."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

from pattern_pilot.core.contracts import ReviewProfile

# Resolve .env relative to the project root (3 levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Pattern Pilot configuration — loaded from env vars / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "postgresql+asyncpg://pp:pp_dev@localhost:5437/pattern_pilot"

    # OpenAI — Responses API
    openai_default_provider: str = "openai"
    openai_api_key: str = ""
    openai_api_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.4"
    openai_reasoning_effort: str = "medium"
    openai_temperature: float = 0.0
    openai_timeout_seconds: int = 300
    openai_reviewer_max_attempts: int = 3
    openai_reviewer_retry_base_seconds: int = 2
    openai_input_cost_per_1m: float | None = None
    openai_output_cost_per_1m: float | None = None

    # Anthropic — Messages API
    anthropic_api_key: str = ""
    anthropic_api_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4-20250514"
    anthropic_timeout_seconds: int = 300

    # QC loop
    pp_max_rounds: int = 3
    pp_default_review_profile: ReviewProfile = ReviewProfile.STANDARD

    # Logging
    pp_log_level: str = "INFO"

    # API
    pp_api_host: str = "0.0.0.0"
    pp_api_port: int = 8100

    # Prompt versioning
    pp_prompt_version: str = "v1.3"

    # Timezone — all PP timestamps use this. Defaults to system local time.
    # Set to an IANA timezone (e.g., "Australia/Sydney", "America/New_York")
    # to override, especially useful for the Docker API container.
    pp_timezone: str = ""

    # Path mapping: Docker↔Host. Comma-separated pairs of "docker_path=host_path".
    # Example: "/projects=/Users/pavanktalari/Projects/AmiTara"
    # When running on host (MCP), Docker paths in the DB are translated to host paths.
    # When running in Docker, no translation needed (paths already correct).
    pp_path_mappings: str = ""

    def resolve_repo_path(self, db_path: str) -> str:
        """Translate a stored repo_path for the current runtime environment.

        If a path mapping matches, replace the prefix. Otherwise return as-is.
        """
        if not self.pp_path_mappings:
            return db_path
        for mapping in self.pp_path_mappings.split(","):
            mapping = mapping.strip()
            if "=" not in mapping:
                continue
            docker_prefix, host_prefix = mapping.split("=", 1)
            docker_prefix = docker_prefix.strip()
            host_prefix = host_prefix.strip()
            # Docker path → host path (MCP server on host)
            if db_path.startswith(docker_prefix):
                return db_path.replace(docker_prefix, host_prefix, 1)
            # Host path → docker path (API in container)
            if db_path.startswith(host_prefix):
                return db_path.replace(host_prefix, docker_prefix, 1)
        return db_path

    def reviewer_api_key(self, provider: str) -> str:
        """Return the configured API key for a reviewer provider."""
        if provider == "openai":
            return self.openai_api_key
        if provider == "anthropic":
            return self.anthropic_api_key
        return ""

    def reviewer_default_model(self, provider: str) -> str:
        """Return the default model for a reviewer provider."""
        if provider == "openai":
            return self.openai_model
        if provider == "anthropic":
            return self.anthropic_model
        return self.openai_model

    def reviewer_base_url(self, provider: str) -> str:
        """Return the base URL for a reviewer provider."""
        if provider == "openai":
            return self.openai_api_base_url
        if provider == "anthropic":
            return self.anthropic_api_base_url
        return self.openai_api_base_url

    def reviewer_timeout_seconds(self, provider: str) -> int:
        """Return the request timeout for a reviewer provider."""
        if provider == "anthropic":
            return self.anthropic_timeout_seconds
        return self.openai_timeout_seconds


def get_settings() -> Settings:
    """Singleton-ish settings loader."""
    return Settings()


def _get_tz() -> ZoneInfo | None:
    """Resolve the configured timezone, or None for system local."""
    tz_name = get_settings().pp_timezone
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (KeyError, Exception):
            return None
    return None


def pp_now() -> datetime:
    """Return the current time in the configured timezone.

    All PP timestamps should use this instead of datetime.utcnow().
    - If PP_TIMEZONE is set: returns timezone-aware datetime in that zone.
    - If PP_TIMEZONE is empty (default): returns local system time (naive),
      which is correct for the MCP server running on the user's Mac.
    """
    tz = _get_tz()
    if tz:
        return datetime.now(tz=tz)
    return datetime.now()
