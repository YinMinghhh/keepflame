"""Ports used by the lifecycle core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, TypeVar

from keepflame.core.models import (
    ActionAttempt,
    FlameState,
    JsonValue,
    KeepResult,
    PendingNotification,
    RunRecord,
)

T = TypeVar("T")


class CapabilityResolver(Protocol):
    def require(self, name: str, expected_type: type[T] | None = None) -> T: ...


class SecretResolver(Protocol):
    def read_text(self, reference: str) -> str: ...


class ArchiveError(ValueError):
    """Evidence or identity cannot safely resolve an expired action intent."""


class AutomationPaused(RuntimeError):
    """Explicit operator intervention is required before another inspection."""


class Storage(Protocol):
    def get_action(self, keeper: str, account_id: str, task: str) -> ActionAttempt | None: ...

    def reserve_action(self, keeper: str, account_id: str, task: str, day: str, now: datetime, *, validate_time=None) -> bool: ...

    def confirm_action(self, keeper: str, account_id: str, task: str, now: datetime, *, day: str, validate_time=None) -> bool: ...

    def is_paused(self, keeper: str, account_id: str, flame_id: str) -> bool: ...

    def pause(self, keeper: str, account_id: str, scope: str, reason: str, now: datetime) -> None: ...

    def resume(self, keeper: str, account_id: str, scope: str) -> None: ...

    def get_state(self, flame_id: str, account_id: str) -> FlameState | None: ...

    def list_states(self) -> list[FlameState]: ...

    def record(
        self, run: RunRecord, state: FlameState,
        notification: PendingNotification | None = None,
    ) -> None: ...

    def get_notification(self, flame_id: str, account_id: str) -> PendingNotification | None: ...

    def update_notification(self, notification: PendingNotification) -> None: ...

    def acknowledge_notification(self, notification: PendingNotification) -> None: ...

    def healthcheck(self) -> None: ...


class Notifier(Protocol):
    def notify(
        self,
        event: str,
        *,
        notification_id: str,
        flame_id: str,
        account_id: str,
        result: KeepResult,
        occurred_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class KeeperContext:
    flame_id: str
    account_id: str
    settings: Mapping[str, JsonValue]
    capabilities: CapabilityResolver
    secrets: SecretResolver
    storage: Storage
    now: datetime
    clock: Clock | None = None


class Keeper(Protocol):
    def keep(self, context: KeeperContext) -> KeepResult: ...


class KeeperRegistry(Protocol):
    def resolve(self, name: str) -> Keeper: ...


class Clock(Protocol):
    def now(self) -> datetime: ...

