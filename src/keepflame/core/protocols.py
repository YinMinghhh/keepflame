"""Ports used by the lifecycle core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, TypeVar

from keepflame.core.models import (
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


class Storage(Protocol):
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


class Keeper(Protocol):
    def keep(self, context: KeeperContext) -> KeepResult: ...


class KeeperRegistry(Protocol):
    def resolve(self, name: str) -> Keeper: ...


class Clock(Protocol):
    def now(self) -> datetime: ...

