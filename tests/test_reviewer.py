"""Tests for OpenAI reviewer — mock API responses, structured parsing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pattern_pilot.core.contracts import (
    FindingTier,
    Verdict,
)
from pattern_pilot.core.reviewer import MalformedReviewerResponse, Reviewer, ReviewerError


def _mock_openai_response(content: dict) -> SimpleNamespace:
    """Build a mock OpenAI Responses API response."""
    return SimpleNamespace(
        output_text=json.dumps(content),
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
    )


def _settings_mock(**overrides: object) -> MagicMock:
    base = {
        "openai_default_provider": "openai",
        "openai_api_key": "test-openai-key",
        "openai_api_base_url": "https://api.openai.com/v1",
        "openai_model": "gpt-4o",
        "openai_reasoning_effort": "medium",
        "openai_timeout_seconds": 300,
        "openai_reviewer_max_attempts": 3,
        "openai_reviewer_retry_base_seconds": 1,
        "openai_input_cost_per_1m": None,
        "openai_output_cost_per_1m": None,
        "anthropic_api_key": "test-anthropic-key",
        "anthropic_api_base_url": "https://api.anthropic.com",
        "anthropic_model": "claude-sonnet-4-20250514",
        "anthropic_timeout_seconds": 300,
        "pp_prompt_version": "v1.0",
    }
    base.update(overrides)
    settings = MagicMock(**base)
    settings.reviewer_api_key.side_effect = (
        lambda provider: settings.openai_api_key
        if provider == "openai"
        else settings.anthropic_api_key
        if provider == "anthropic"
        else ""
    )
    settings.reviewer_default_model.side_effect = (
        lambda provider: settings.openai_model
        if provider == "openai"
        else settings.anthropic_model
        if provider == "anthropic"
        else settings.openai_model
    )
    settings.reviewer_timeout_seconds.side_effect = (
        lambda provider: settings.openai_timeout_seconds
        if provider == "openai"
        else settings.anthropic_timeout_seconds
        if provider == "anthropic"
        else settings.openai_timeout_seconds
    )
    settings.reviewer_base_url.side_effect = (
        lambda provider: settings.openai_api_base_url
        if provider == "openai"
        else settings.anthropic_api_base_url
        if provider == "anthropic"
        else settings.openai_api_base_url
    )
    return settings


@pytest.fixture
def mock_reviewer():
    """Reviewer with mocked OpenAI client."""
    with patch("pattern_pilot.core.reviewer.get_settings") as mock_settings:
        mock_settings.return_value = _settings_mock(openai_api_key="test-key")
        reviewer = Reviewer(api_key="test-key")
        return reviewer


class TestReviewer:
    def test_reviewer_accepts_injected_model_and_reasoning_effort(self):
        with patch("pattern_pilot.core.reviewer.get_settings") as mock_settings:
            mock_settings.return_value = _settings_mock(
                openai_api_key="test-key",
                openai_model="gpt-5.4",
            )

            reviewer = Reviewer(
                api_key="test-key",
                provider="openai",
                model="gpt-4o",
                reasoning_effort="high",
            )

        assert reviewer.provider == "openai"
        assert reviewer.model == "gpt-4o"
        assert reviewer.reasoning_effort == "high"

    def test_openai_reviewer_uses_configured_base_url(self):
        with patch("pattern_pilot.core.reviewer.get_settings") as mock_settings:
            mock_settings.return_value = _settings_mock(
                openai_api_key="test-key",
                openai_api_base_url="https://example.openai-proxy.local/v1",
            )
            with patch("pattern_pilot.core.reviewer.AsyncOpenAI") as mock_openai:
                Reviewer(
                    api_key="test-key",
                    provider="openai",
                    model="gpt-4o",
                )

        mock_openai.assert_called_once_with(
            api_key="test-key",
            base_url="https://example.openai-proxy.local/v1",
        )

    def test_reviewer_accepts_anthropic_provider_and_alias_model(self):
        with patch("pattern_pilot.core.reviewer.get_settings") as mock_settings:
            mock_settings.return_value = _settings_mock(
                openai_default_provider="anthropic",
                anthropic_api_key="anthropic-key",
            )

            reviewer = Reviewer(
                provider="anthropic",
                model="claude-sonnet-4",
            )

        assert reviewer.provider == "anthropic"
        assert reviewer.model == "claude-sonnet-4-20250514"
        assert reviewer.reasoning_effort == "medium"

    def test_anthropic_messages_url_uses_configured_base_url(self):
        with patch("pattern_pilot.core.reviewer.get_settings") as mock_settings:
            mock_settings.return_value = _settings_mock(
                openai_default_provider="anthropic",
                anthropic_api_key="anthropic-key",
                anthropic_api_base_url="https://anthropic-proxy.local/custom",
            )

            reviewer = Reviewer(
                provider="anthropic",
                model="claude-sonnet-4",
            )

        assert reviewer._anthropic_messages_url() == (
            "https://anthropic-proxy.local/custom/v1/messages"
        )

    @pytest.mark.asyncio
    async def test_clean_pass(self, mock_reviewer: Reviewer, sample_context_bundle):
        response_data = {"verdict": "pass", "findings": []}
        mock_reviewer.client.responses.create = AsyncMock(
            return_value=_mock_openai_response(response_data)
        )

        result = await mock_reviewer.review(sample_context_bundle)
        mock_reviewer.client.responses.create.assert_awaited_once()
        assert result.verdict == Verdict.PASS
        assert len(result.findings) == 0
        assert result.tokens_in == 1000
        assert result.tokens_out == 500

    @pytest.mark.asyncio
    async def test_blocking_finding(self, mock_reviewer: Reviewer, sample_context_bundle):
        response_data = {
            "verdict": "blocking",
            "findings": [
                {
                    "tier": "blocking",
                    "category": "security",
                    "file_path": "main.py",
                    "line_start": 1,
                    "message": "Print statement in production",
                    "suggestion": "Use logging",
                    "autofix_safe": True,
                }
            ],
        }
        mock_reviewer.client.responses.create = AsyncMock(
            return_value=_mock_openai_response(response_data)
        )

        result = await mock_reviewer.review(sample_context_bundle)
        mock_reviewer.client.responses.create.assert_awaited_once()
        assert result.verdict == Verdict.BLOCKING
        assert len(result.findings) == 1
        assert result.findings[0].tier == FindingTier.BLOCKING
        assert result.findings[0].autofix_safe is True

    @pytest.mark.asyncio
    async def test_pass_with_advisories(
        self, mock_reviewer: Reviewer, sample_context_bundle
    ):
        response_data = {
            "verdict": "pass_with_advisories",
            "findings": [
                {
                    "tier": "advisory",
                    "category": "documentation",
                    "file_path": "utils.py",
                    "message": "Consider adding docstrings",
                    "autofix_safe": False,
                }
            ],
        }
        mock_reviewer.client.responses.create = AsyncMock(
            return_value=_mock_openai_response(response_data)
        )

        result = await mock_reviewer.review(sample_context_bundle)
        mock_reviewer.client.responses.create.assert_awaited_once()
        assert result.verdict == Verdict.PASS_WITH_ADVISORIES
        assert result.findings[0].tier == FindingTier.ADVISORY

    @pytest.mark.asyncio
    async def test_malformed_json_raises_reviewer_error(
        self, mock_reviewer: Reviewer, sample_context_bundle
    ):
        """If OpenAI returns garbage, fail as infrastructure, not code review."""
        response = SimpleNamespace(
            output_text="not valid json at all",
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )

        mock_reviewer.client.responses.create = AsyncMock(return_value=response)

        with pytest.raises(MalformedReviewerResponse):
            await mock_reviewer.review(sample_context_bundle)
        mock_reviewer.client.responses.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_chat_usage_token_fallback(
        self, mock_reviewer: Reviewer, sample_context_bundle
    ):
        response_data = {"verdict": "pass", "findings": []}
        response = SimpleNamespace(
            output_text=json.dumps(response_data),
            usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45),
        )
        mock_reviewer.client.responses.create = AsyncMock(return_value=response)

        result = await mock_reviewer.review(sample_context_bundle)
        mock_reviewer.client.responses.create.assert_awaited_once()
        assert result.tokens_in == 123
        assert result.tokens_out == 45

    @pytest.mark.asyncio
    async def test_cost_estimate(self, mock_reviewer: Reviewer):
        mock_reviewer.input_cost_per_1m = 2.50
        mock_reviewer.output_cost_per_1m = 10.0
        cost = mock_reviewer._estimate_cost(1_000_000, 100_000)
        assert cost > 0
        assert cost == 2.50 + 1.0  # $2.50 input + $1.00 output

    @pytest.mark.asyncio
    async def test_cost_unavailable_without_configured_rates(
        self, mock_reviewer: Reviewer
    ):
        assert mock_reviewer._estimate_cost(1_000_000, 100_000) is None

    @pytest.mark.asyncio
    async def test_reviewer_retries_and_raises_retryable_error(
        self, mock_reviewer: Reviewer, sample_context_bundle
    ):
        mock_reviewer.client.responses.create = AsyncMock(
            side_effect=RuntimeError("upstream timeout")
        )

        with patch(
            "pattern_pilot.core.reviewer.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep, pytest.raises(ReviewerError) as exc_info:
            await mock_reviewer.review(sample_context_bundle)

        assert "unavailable after 3 attempts" in str(exc_info.value)
        assert mock_reviewer.client.responses.create.await_count == 3
        assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_anthropic_clean_pass(self, sample_context_bundle):
        response_data = {"verdict": "pass", "findings": []}
        anthropic_response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "content": [{"type": "text", "text": json.dumps(response_data)}],
                "usage": {"input_tokens": 321, "output_tokens": 123},
            },
        )
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = anthropic_response

        with patch("pattern_pilot.core.reviewer.get_settings") as mock_settings:
            mock_settings.return_value = _settings_mock(
                openai_default_provider="anthropic",
                anthropic_api_key="anthropic-key",
            )
            with patch("pattern_pilot.core.reviewer.httpx.AsyncClient") as mock_httpx:
                mock_httpx.return_value = mock_client
                reviewer = Reviewer(provider="anthropic")
                result = await reviewer.review(sample_context_bundle)

        mock_client.post.assert_awaited_once()
        assert result.verdict == Verdict.PASS
        assert result.tokens_in == 321
        assert result.tokens_out == 123
        assert result.cost_usd is None
