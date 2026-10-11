"""Long-running scheduler for configured Flame inspection windows."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from keepflame.core.config import AppConfig, FlameConfig
from keepflame.core.models import KeepResult, KeepStatus, RunOutcome
from keepflame.core.orchestrator import Orchestrator, SystemClock
from keepflame.core.protocols import Clock, Storage, StorageError
from keepflame.core.eligibility import execution_paused
from keepflame.core.schedule import due_at


class SchedulerService:
    def __init__(
        self,
        *,
        config: AppConfig,
        storage: Storage,
        orchestrator: Orchestrator,
        clock: Clock | None = None,
        stop_event: threading.Event | None = None,
        before_tick=None,
        blocked=None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.orchestrator = orchestrator
        self.clock = clock or SystemClock()
        self.stop_event = stop_event or threading.Event()
        self.before_tick = before_tick or (lambda: None)
        self.blocked = blocked or (lambda flame: False)
        # One storage_corrupt alert per Flame/account/UTC-day within this process.
        self._storage_fault_days: set[tuple[str, str, str]] = set()

    def run_due(self, *, now: datetime | None = None) -> tuple[list[RunOutcome], datetime | None]:
        current = (now or self.clock.now()).astimezone(timezone.utc)
        try:
            self.before_tick()
        except StorageError as exc:
            self._report_storage_corrupt(exc, flame=None, now=current)
            return [], current + timedelta(seconds=60)

        outcomes: list[RunOutcome] = []
        for flame in self.config.enabled_flames():
            current = (self.clock.now() if outcomes or now is None else now).astimezone(timezone.utc)
            if self.stop_event.is_set():
                break
            try:
                if self.blocked(flame) or execution_paused(
                        self.storage, flame.keeper, flame.account_id, flame.id, now=current):
                    continue
                state = self.storage.get_state(flame.id, flame.account_id)
                if due_at(flame.schedule, state, current) <= current:
                    outcomes.append(self.orchestrator.run_flame(flame, now=current))
            except StorageError as exc:
                self._report_storage_corrupt(exc, flame=flame, now=current)
                continue

        current = self.clock.now().astimezone(timezone.utc)
        next_due: datetime | None = None
        for flame in self.config.enabled_flames():
            try:
                notification_due = self.orchestrator.deliver_pending(flame, now=current)
                if notification_due is not None and (next_due is None or notification_due < next_due):
                    next_due = notification_due
                if self.stop_event.is_set() or self.blocked(flame) or execution_paused(
                        self.storage, flame.keeper, flame.account_id, flame.id, now=current):
                    continue
                state = self.storage.get_state(flame.id, flame.account_id)
                candidate = due_at(flame.schedule, state, current)
                if next_due is None or candidate < next_due:
                    next_due = candidate
            except StorageError as exc:
                self._report_storage_corrupt(exc, flame=flame, now=current)
                continue
        return outcomes, next_due

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                _, next_due = self.run_due()
            except StorageError as exc:
                # run_due already isolates per-Flame faults; this is a last resort.
                now = self.clock.now().astimezone(timezone.utc)
                self._report_storage_corrupt(exc, flame=None, now=now)
                next_due = now + timedelta(seconds=60)
            if next_due is None:
                timeout = 5.0
            else:
                seconds = (next_due - self.clock.now().astimezone(timezone.utc)).total_seconds()
                timeout = min(max(seconds, 1.0), 5.0)
            self.stop_event.wait(timeout)

    def _report_storage_corrupt(
        self,
        exc: StorageError,
        *,
        flame: FlameConfig | None,
        now: datetime,
    ) -> None:
        """Emit one typed alert and skip further dispatch for the affected scope."""
        if flame is None:
            flame_id, account_id, zone = "*", "*", "UTC"
        else:
            flame_id, account_id, zone = flame.id, flame.account_id, flame.schedule.timezone
        try:
            day = now.astimezone(ZoneInfo(zone) if zone != "UTC" else timezone.utc).date().isoformat()
        except Exception:
            day = now.astimezone(timezone.utc).date().isoformat()
        key = (flame_id, account_id, day)
        if key in self._storage_fault_days:
            return
        self._storage_fault_days.add(key)
        result = KeepResult(
            status=KeepStatus.FAILED,
            message="Stored Flame state is corrupt and was not applied",
            metadata={"error_type": type(exc).__name__, "reason": "storage_corrupt"},
        )
        try:
            self.orchestrator.notifier.notify(
                "storage_corrupt",
                notification_id=str(uuid4()),
                flame_id=flame_id,
                account_id=account_id,
                result=result,
                occurred_at=now,
            )
        except Exception:
            # Alert failure must not take down the daemon.
            return
