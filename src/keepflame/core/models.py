"""Normalized domain models shared by Core and Keepers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
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


# A FAILED result whose metadata carries ``failure_class == FAILURE_CLASS_TRANSPORT`` got no
# HTTP response at all and is safe to try again later (no credited action can have landed
# unnoticed, because the next run re-reads remote state first). The core then spends the
# separate per-day network budget instead of the business ``retry_minutes`` ladder.
FAILURE_CLASS_TRANSPORT = "transport"


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
class BusinessTarget:
    """Immutable daily scope selected before any Keeper operation."""
    day: str
    timezone: str

    def __post_init__(self) -> None:
        if date.fromisoformat(self.day).isoformat() != self.day:
            raise ValueError("business day must be canonical ISO format")
        ZoneInfo(self.timezone)

    @classmethod
    def at(cls, instant: datetime, zone: str) -> BusinessTarget:
        _require_aware(instant, "business instant")
        return cls(instant.astimezone(ZoneInfo(zone)).date().isoformat(), zone)

    def contains(self, instant: datetime) -> bool:
        return self == self.at(instant, self.timezone)

    def to_dict(self) -> dict[str, str]:
        return {"day": self.day, "timezone": self.timezone}


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    """A successful observation, captured before resource cleanup begins."""
    target: BusinessTarget
    observed_at: datetime
    source: str = "keeper_observation"

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if not self.target.contains(self.observed_at):
            raise ValueError("completion observation does not belong to its target")
        if self.source not in {"keeper_observation", "core_return"}:
            raise ValueError("invalid completion source")

    def to_dict(self) -> dict:
        return {"target": self.target.to_dict(),
                "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
                "source": self.source}

    @classmethod
    def from_dict(cls, raw: dict) -> CompletionReceipt:
        return cls(BusinessTarget(**raw["target"]), datetime.fromisoformat(raw["observed_at"]), raw["source"])


@dataclass(frozen=True, slots=True)
class KeepResult:
    status: KeepStatus
    message: str
    reward: Reward | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    next_action_at: datetime | None = None
    human_action: HumanAction | None = None
    receipt: CompletionReceipt | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("keep result message must be non-empty")
        if self.status is KeepStatus.WAITING_HUMAN and self.human_action is None:
            raise ValueError("WAITING_HUMAN requires a human action")
        if self.status is not KeepStatus.WAITING_HUMAN and self.human_action is not None:
            raise ValueError("only WAITING_HUMAN may carry a human action")
        if self.receipt is not None and self.status is not KeepStatus.SUCCESS:
            raise ValueError("only SUCCESS may carry a completion receipt")
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
    last_checked_target: BusinessTarget | None = None
    last_success_receipt: CompletionReceipt | None = None

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
    business_target: BusinessTarget | None = None
    clock_regressed: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        _require_aware(self.finished_at, "finished_at")
        if self.result.receipt is not None and self.result.receipt.target != self.business_target:
            raise ValueError("run and receipt have different business targets")
        if self.finished_at < self.started_at and not self.clock_regressed:
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
    business_target: BusinessTarget | None = None

    def __post_init__(self) -> None:
        for name in ("occurred_at", "retry_at", "reminder_at"):
            _require_aware(getattr(self, name), name)
        if self.attempts < 0:
            raise ValueError("notification attempts cannot be negative")
