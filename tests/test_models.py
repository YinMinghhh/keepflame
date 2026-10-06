from datetime import datetime, timezone

import pytest

from keepflame.core.models import HumanAction, KeepResult, KeepStatus


def test_waiting_human_requires_action() -> None:
    with pytest.raises(ValueError, match="requires a human action"):
        KeepResult(status=KeepStatus.WAITING_HUMAN, message="waiting")


def test_other_status_cannot_carry_action() -> None:
    action = HumanAction(kind="DO", summary="Do it", instructions="Do it safely")
    with pytest.raises(ValueError, match="only WAITING_HUMAN"):
        KeepResult(status=KeepStatus.SUCCESS, message="ok", human_action=action)


@pytest.mark.parametrize("key", ["cookie", "apiToken", "session_id", "passwordHash"])
def test_sensitive_metadata_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="sensitive metadata key"):
        KeepResult(status=KeepStatus.SUCCESS, message="ok", metadata={key: "never"})


def test_next_action_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        KeepResult(
            status=KeepStatus.SUCCESS,
            message="ok",
            next_action_at=datetime(2026, 8, 11, 9, 0),
        )
    KeepResult(
        status=KeepStatus.SUCCESS,
        message="ok",
        next_action_at=datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc),
    )

