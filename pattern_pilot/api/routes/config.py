"""Configuration discovery routes for UI clients."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from pattern_pilot.core.config import get_settings

router = APIRouter()


class ReviewerProviderResponse(BaseModel):
    id: str
    label: str
    models: list[str]
    supports_reasoning_effort: bool
    available: bool = True


class ProviderConfigResponse(BaseModel):
    default_provider: str
    default_model: str
    default_reasoning_effort: str
    providers: list[ReviewerProviderResponse]


@router.get("/config/providers", response_model=ProviderConfigResponse)
async def get_provider_config() -> ProviderConfigResponse:
    settings = get_settings()
    default_provider = settings.openai_default_provider
    return ProviderConfigResponse(
        default_provider=default_provider,
        default_model=settings.reviewer_default_model(default_provider),
        default_reasoning_effort=settings.openai_reasoning_effort,
        providers=[
            ReviewerProviderResponse(
                id="openai",
                label="OpenAI",
                models=[
                    "gpt-5.4",
                    "gpt-5.4-mini",
                    "gpt-5-mini",
                    "gpt-4o",
                    "o3",
                ],
                supports_reasoning_effort=True,
                available=True,
            ),
            ReviewerProviderResponse(
                id="anthropic",
                label="Anthropic",
                models=["claude-opus-4", "claude-sonnet-4"],
                supports_reasoning_effort=False,
                available=True,
            ),
            ReviewerProviderResponse(
                id="google",
                label="Gemini",
                models=["gemini-2.5-pro", "gemini-2.5-flash"],
                supports_reasoning_effort=False,
                available=True,
            ),
            ReviewerProviderResponse(
                id="perplexity",
                label="Perplexity",
                models=["sonar-pro", "sonar"],
                supports_reasoning_effort=False,
                available=True,
            ),
        ],
    )
