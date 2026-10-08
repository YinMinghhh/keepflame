"""Long-running scheduler for configured Flame inspection windows."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from keepflame.core.config import AppConfig
from keepflame.core.models import RunOutcome
from keepflame.core.orchestrator import Orchestrator, SystemClock
from keepflame.core.protocols import Clock, Storage
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

    def run_due(self, *, now: datetime | None = None) -> tuple[list[RunOutcome], datetime | None]:
        self.before_tick()
        current = (now or self.clock.now()).astimezone(timezone.utc)
        outcomes: list[RunOutcome] = []
        for flame in self.config.enabled_flames():
            current = (self.clock.now() if outcomes or now is None else now).astimezone(timezone.utc)
            if self.stop_event.is_set() or self.blocked(flame) or self.storage.is_paused(flame.keeper, flame.account_id, flame.id):
                continue
            state = self.storage.get_state(flame.id, flame.account_id)
            if due_at(flame.schedule, state, current) <= current:
                outcomes.append(self.orchestrator.run_flame(flame, now=current))

        current = self.clock.now().astimezone(timezone.utc)
        next_due: datetime | None = None
        for flame in self.config.enabled_flames():
            notification_due = self.orchestrator.deliver_pending(flame, now=current)
            if notification_due is not None and (next_due is None or notification_due < next_due):
                next_due = notification_due
            if self.stop_event.is_set() or self.blocked(flame) or self.storage.is_paused(flame.keeper, flame.account_id, flame.id):
                continue
            state = self.storage.get_state(flame.id, flame.account_id)
            candidate = due_at(flame.schedule, state, current)
            if next_due is None or candidate < next_due:
                next_due = candidate
        return outcomes, next_due

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            _, next_due = self.run_due()
            if next_due is None:
                timeout = 5.0
            else:
                seconds = (next_due - self.clock.now().astimezone(timezone.utc)).total_seconds()
                timeout = min(max(seconds, 1.0), 5.0)
            self.stop_event.wait(timeout)

