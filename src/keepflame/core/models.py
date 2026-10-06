"""Normalized domain models shared by Core and Keepers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
)


@dataclass(frozen=True, slots=True)
class ActionAttempt:
    """Durable write intent; an unresolved attempt remains blocked across days."""
    state: str
    day: str
    started_at: datetime
    confirmed_at: datetime | None = None


class KeepStatus(StrEnum):
    """The normalized outcome of one Keeper run."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    WAITING_HUMAN = "WAITING_HUMAN"


class RewardSource(StrEnum):
    """Whether a reward was observed remotely or claimed by this run."""

    OBSERVED = "observed"
    CLAIMED = "claimed"


@dataclass(frozen=True, slots=True)
class Reward:
    amount: str
    unit: str
    source: RewardSource

    def __post_init__(self) -> None:
        if not self.amount.strip() or not self.unit.strip():
            raise ValueError("reward amount and unit must be non-empty")

    def to_dict(self) -> dict[str, str]:
        return {
            "amount": self.amount,
            "unit": self.unit,
            "source": self.source.value,
        }


@dataclass(frozen=True, slots=True)
class HumanAction:
    kind: str
    summary: str
    instructions: str
    url: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("kind", self.kind),
            ("summary", self.summary),
            ("instructions", self.instructions),
        ):
            if not value.strip():
                raise ValueError(f"human action {name} must be non-empty")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "instructions": self.instructions,
            "url": self.url,
        }


def _validate_metadata(value: JsonValue, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = key.casefold()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(f"sensitive metadata key is forbidden: {path}.{key}")
            _validate_metadata(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_metadata(child, f"{path}[{index}]")
        return
    if value is not None and not isinstance(value, (bool, int, float, str)):
        raise TypeError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _require_aware(value: datetime | None, field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class KeepResult:
    status: KeepStatus
    message: str
    reward: Reward | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    next_action_at: datetime | None = None
    human_action: HumanAction | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("keep result message must be non-empty")
        if self.status is KeepStatus.WAITING_HUMAN and self.human_action is None:
            raise ValueError("WAITING_HUMAN requires a human action")
        if self.status is not KeepStatus.WAITING_HUMAN and self.human_action is not None:
            raise ValueError("only WAITING_HUMAN may carry a human action")
        _require_aware(self.next_action_at, "next_action_at")
        metadata_copy = dict(self.metadata)
        _validate_metadata(metadata_copy)
        object.__setattr__(self, "metadata", metadata_copy)


@dataclass(frozen=True, slots=True)
class FlameState:
    flame_id: str
    account_id: str
    status: KeepStatus
    message: str
    last_checked_at: datetime
    last_success_at: datetime | None
    consecutive_failures: int
    pending_human_since: datetime | None
    human_action: HumanAction | None
    next_action_at: datetime | None
    last_reminder_at: datetime | None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "last_checked_at",
            "last_success_at",
            "pending_human_since",
            "next_action_at",
            "last_reminder_at",
        ):
            _require_aware(getattr(self, name), name)
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures cannot be negative")
        _validate_metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    flame_id: str
    account_id: str
    started_at: datetime
    finished_at: datetime
    result: KeepResult

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        _require_aware(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be before started_at")


@dataclass(frozen=True, slots=True)
class RunOutcome:
    record: RunRecord
    state: FlameState
    notification_event: str | None



@dataclass(frozen=True, slots=True)
class PendingNotification:
    """Latest undelivered lifecycle event for one Flame/account."""

    notification_id: str
    flame_id: str
    account_id: str
    event: str
    result: KeepResult
    occurred_at: datetime
    retry_at: datetime | None
    reminder_at: datetime | None = None
    attempts: int = 0
    last_error_type: str | None = None

    def __post_init__(self) -> None:
        for name in ("occurred_at", "retry_at", "reminder_at"):
            _require_aware(getattr(self, name), name)
        if self.attempts < 0:
            raise ValueError("notification attempts cannot be negative")
