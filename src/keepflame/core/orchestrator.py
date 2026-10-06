"""Lifecycle orchestration for one normalized Keeper run."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from keepflame.core.config import FlameConfig
from keepflame.core.models import (
    FlameState,
    KeepResult,
    KeepStatus,
    RunOutcome,
    RunRecord,
)
from keepflame.core.protocols import (
    CapabilityResolver,
    Clock,
    KeeperContext,
    KeeperRegistry,
    Notifier,
    SecretResolver,
    Storage,
)
from keepflame.core.schedule import next_after_result, reminder_due


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class Orchestrator:
    def __init__(
        self,
        *,
        storage: Storage,
        registry: KeeperRegistry,
        capabilities: CapabilityResolver,
        secrets: SecretResolver,
        notifier: Notifier,
        clock: Clock | None = None,
    ) -> None:
        self.storage = storage
        self.registry = registry
        self.capabilities = capabilities
        self.secrets = secrets
        self.notifier = notifier
        self.clock = clock or SystemClock()

    def run_flame(
        self,
        flame: FlameConfig,
        *,
        now: datetime | None = None,
        notify: bool = True,
    ) -> RunOutcome:
        started_at = (now or self.clock.now()).astimezone(timezone.utc)
        previous = self.storage.get_state(flame.id, flame.account_id)
        try:
            keeper = self.registry.resolve(flame.keeper)
            context = KeeperContext(
                flame_id=flame.id,
                account_id=flame.account_id,
                settings=flame.settings,
                capabilities=self.capabilities,
                secrets=self.secrets,
                storage=self.storage,
                now=started_at,
            )
            result = keeper.keep(context)
        except Exception as exc:
            result = KeepResult(
                status=KeepStatus.FAILED,
                message="Keeper execution failed safely",
                metadata={"error_type": type(exc).__name__},
            )

        finished_at = (now or self.clock.now()).astimezone(timezone.utc)
        result = replace(
            result,
            next_action_at=next_after_result(flame.schedule, finished_at, result.status),
        )
        event, mark_reminder = self._notification(
            flame,
            previous,
            result,
            finished_at,
        )
        state = self._transition(
            flame,
            previous,
            result,
            finished_at,
            mark_reminder=mark_reminder,
        )
        record = RunRecord(
            run_id=str(uuid4()),
            flame_id=flame.id,
            account_id=flame.account_id,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
        )
        self.storage.record(record, state)
        if event is not None and notify:
            self.notifier.notify(
                event,
                flame_id=flame.id,
                account_id=flame.account_id,
                result=result,
                occurred_at=finished_at,
            )
        return RunOutcome(record=record, state=state, notification_event=event)

    @staticmethod
    def _transition(
        flame: FlameConfig,
        previous: FlameState | None,
        result: KeepResult,
        checked_at: datetime,
        *,
        mark_reminder: bool,
    ) -> FlameState:
        if result.status is KeepStatus.SUCCESS:
            last_success = checked_at
            failures = 0
            pending_since = None
            action = None
            last_reminder = None
        elif result.status is KeepStatus.FAILED:
            last_success = None if previous is None else previous.last_success_at
            failures = (0 if previous is None else previous.consecutive_failures) + 1
            pending_since = None
            action = None
            last_reminder = None
        else:
            last_success = None if previous is None else previous.last_success_at
            failures = 0
            same_action = (
                previous is not None
                and previous.status is KeepStatus.WAITING_HUMAN
                and previous.human_action is not None
                and result.human_action is not None
                and previous.human_action.kind == result.human_action.kind
            )
            pending_since = (
                previous.pending_human_since if same_action else checked_at
            )
            action = result.human_action
            if mark_reminder:
                last_reminder = checked_at
            elif same_action:
                last_reminder = previous.last_reminder_at
            else:
                last_reminder = None

        return FlameState(
            flame_id=flame.id,
            account_id=flame.account_id,
            status=result.status,
            message=result.message,
            last_checked_at=checked_at,
            last_success_at=last_success,
            consecutive_failures=failures,
            pending_human_since=pending_since,
            human_action=action,
            next_action_at=result.next_action_at,
            last_reminder_at=last_reminder,
            metadata=dict(result.metadata),
        )

    @classmethod
    def _notification(
        cls,
        flame: FlameConfig,
        previous: FlameState | None,
        result: KeepResult,
        now: datetime,
    ) -> tuple[str | None, bool]:
        if result.status is KeepStatus.WAITING_HUMAN:
            action_changed = (
                previous is None
                or previous.status is not KeepStatus.WAITING_HUMAN
                or previous.human_action is None
                or result.human_action is None
                or previous.human_action.kind != result.human_action.kind
            )
            new_schedule_day = cls._is_new_schedule_day(flame, previous, now)
            if action_changed or new_schedule_day:
                return "human_action_required", cls._at_or_after_reminder(flame, now)
            if reminder_due(flame.schedule, previous, now):
                return "human_action_reminder", True
            return None, False

        if result.status is KeepStatus.FAILED:
            if (
                previous is None
                or previous.status is not KeepStatus.FAILED
                or previous.message != result.message
            ):
                return "flame_failed", False
            return None, False

        if previous is not None and previous.status is not KeepStatus.SUCCESS:
            return "flame_recovered", False
        return None, False

    @staticmethod
    def _is_new_schedule_day(
        flame: FlameConfig,
        previous: FlameState | None,
        now: datetime,
    ) -> bool:
        if previous is None:
            return True
        zone = ZoneInfo(flame.schedule.timezone)
        return previous.last_checked_at.astimezone(zone).date() != now.astimezone(zone).date()

    @staticmethod
    def _at_or_after_reminder(flame: FlameConfig, now: datetime) -> bool:
        zone = ZoneInfo(flame.schedule.timezone)
        return now.astimezone(zone).time() >= flame.schedule.reminder_at
