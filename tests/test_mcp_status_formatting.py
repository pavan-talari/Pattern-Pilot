from pattern_pilot.mcp_server import (
    _format_deterministic_failure,
    _format_reviewer_error,
    _format_round_limit_reached,
)


def test_format_deterministic_failure_labels_target_project_failure() -> None:
    lines = _format_deterministic_failure(
        {
            "phase": "deterministic_checks",
            "checks": [
                {
                    "check_name": "lint",
                    "passed": False,
                    "output": "backend/app/main.py:1:1 I001 import block is un-sorted",
                    "duration_ms": 18,
                }
            ],
        }
    )

    text = "\n".join(lines)

    assert "Target project deterministic checks failed before LLM review." in text
    assert "not a Pattern Pilot infrastructure failure" in text
    assert "`lint` failed (18ms)" in text
    assert "I001 import block is un-sorted" in text


def test_format_round_limit_reached_explains_no_new_review() -> None:
    lines = _format_round_limit_reached(
        {
            "max_rounds": 5,
            "last_completed_round": 5,
            "review_attempted": False,
        }
    )

    text = "\n".join(lines)

    assert "Max review rounds were already exhausted" in text
    assert "No new LLM round was run" in text
    assert "last completed round" in text.lower()


def test_format_reviewer_error_marks_infrastructure_issue() -> None:
    lines = _format_reviewer_error(
        {
            "error": "OpenAI reviewer unavailable after 3 attempts. RuntimeError: upstream timeout",
            "retryable": True,
        }
    )

    text = "\n".join(lines)

    assert "Reviewer infrastructure error" in text
    assert "target code was not reviewed" in text.lower()
    assert "retryable" in text.lower()
