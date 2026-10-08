"""Lifecycle orchestration for one normalized Keeper run."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from keepflame.core.config import FlameConfig
from keepflame.core.models import (
    BusinessTarget,
    CompletionReceipt,
    FlameState,
    KeepResult,
    KeepStatus,
    PendingNotification,
    RunOutcome,
    RunRecord,
)
from keepflame.core.protocols import (
    AutomationPaused,
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
        business_target: BusinessTarget | None = None,
    ) -> RunOutcome:
        if self.storage.is_paused(flame.keeper, flame.account_id, flame.id):
            raise AutomationPaused("automation is paused; inspect status and resume explicitly")
        started_at = (now or self.clock.now()).astimezone(timezone.utc)
        target = business_target or BusinessTarget.at(started_at, flame.schedule.timezone)
        issued: list[CompletionReceipt] = []
        def issue_receipt() -> CompletionReceipt:
            observed = self.clock.now().astimezone(timezone.utc)
            if observed < started_at or not target.contains(observed):
                raise ValueError("completion outside admitted business target")
            receipt = CompletionReceipt(target, observed)
            issued.append(receipt)
            return receipt
        previous = self.storage.get_state(flame.id, flame.account_id)
        pending = self.storage.get_notification(flame.id, flame.account_id)
        try:
            keeper = self.registry.resolve(flame.keeper)
            if (target.timezone != flame.schedule.timezone
                    or getattr(keeper, "business_timezone", target.timezone) != target.timezone
                    or not target.contains(self.clock.now())):
                raise ValueError("Keeper business target differs from the admitted schedule")
            context = KeeperContext(
                flame_id=flame.id,
                account_id=flame.account_id,
                settings=flame.settings,
                capabilities=self.capabilities,
                secrets=self.secrets,
                storage=self.storage,
                now=started_at,
                clock=self.clock,
                business_target=target,
                completion_receipt=issue_receipt,
            )
            result = keeper.keep(context)
        except Exception as exc:
            result = KeepResult(
                status=KeepStatus.FAILED,
                message="Keeper execution failed safely",
                metadata={"error_type": type(exc).__name__},
            )

        # Always sample the live clock after cleanup; an admission-time `now`
        # must never freeze the whole scheduler run's timestamps.
        finished_at = self.clock.now().astimezone(timezone.utc)
        if result.status is KeepStatus.SUCCESS:
            if result.receipt is None and target.contains(finished_at) and finished_at >= started_at:
                result = replace(result, receipt=CompletionReceipt(target, finished_at, "core_return"))
            elif result.receipt is None or not any(result.receipt is receipt for receipt in issued):
                result = KeepResult(KeepStatus.FAILED, "Completion requires a valid current-run receipt",
                    metadata={**result.metadata, "automation_paused": True, "reason": "completion_receipt_missing_or_invalid"},
                    next_action_at=result.next_action_at)
        clock_regressed = (finished_at < started_at or
            result.receipt is not None and finished_at < result.receipt.observed_at)
        if clock_regressed:
            result = replace(result, metadata={**result.metadata, "automation_paused": True,
                             "reason": "clock_regressed"})
        same_target = previous is not None and previous.last_checked_target == target
        failures = (previous.consecutive_failures if same_target else 0) + 1
        result = replace(
            result,
            metadata={**result.metadata, "business_target": target.to_dict()},
            next_action_at=next_after_result(
                flame.schedule, finished_at, result.status,
                requested_at=result.next_action_at,
                consecutive_failures=failures,
                completed_target=None if result.receipt is None else result.receipt.target,
            ),
        )
        event, mark_reminder = self._notification(
            flame,
            previous,
            result,
            finished_at,
            target,
        )
        state = self._transition(
            flame,
            previous,
            result,
            finished_at,
            target,
        )
        record = RunRecord(
            run_id=str(uuid4()),
            flame_id=flame.id,
            account_id=flame.account_id,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            business_target=target,
            clock_regressed=clock_regressed,
        )
        same_pending = (
            pending is not None
            and pending.result.status is result.status
            and pending.result.message == result.message
            and pending.result.human_action == result.human_action
            and (pending.business_target == target or event is None)
            and (event is None or event == pending.event)
        )
        if same_pending:
            if pending.business_target == target:
                pending = replace(pending, result=result)
        elif event is not None:
            pending = PendingNotification(
                notification_id=str(uuid4()), flame_id=flame.id, account_id=flame.account_id,
                event=event, result=result, occurred_at=finished_at, retry_at=finished_at,
                reminder_at=finished_at if mark_reminder else None,
                business_target=target,
            )
        else:
            pending = None
        if result.metadata.get("automation_paused") is True:
            scope = "*" if result.metadata.get("pause_account") is True else flame.id
            self.storage.pause(flame.keeper, flame.account_id, scope,
                               str(result.metadata.get("reason", "human_action_required")), finished_at)
        self.storage.record(record, state, pending)
        if notify:
            self.deliver_pending(flame, now=finished_at)
            state = self.storage.get_state(flame.id, flame.account_id) or state
        return RunOutcome(record=record, state=state, notification_event=event)

    def deliver_pending(self, flame: FlameConfig, *, now: datetime) -> datetime | None:
        """Bounded delivery retries are independent of remote status checks."""
        pending = self.storage.get_notification(flame.id, flame.account_id)
        if pending is None or pending.retry_at is None:
            return None
        if pending.retry_at > now:
            return pending.retry_at
        try:
            self.notifier.notify(
                pending.event, notification_id=pending.notification_id,
                flame_id=flame.id, account_id=flame.account_id,
                result=pending.result, occurred_at=pending.occurred_at,
            )
        except Exception as exc:
            attempts = pending.attempts + 1
            retry_at = None if attempts >= 5 else now + timedelta(seconds=60 * 2 ** (attempts - 1))
            self.storage.update_notification(replace(
                pending, attempts=attempts, retry_at=retry_at, last_error_type=type(exc).__name__,
            ))
            return retry_at
        self.storage.acknowledge_notification(pending)
        return None

    @staticmethod
    def _transition(
        flame: FlameConfig,
        previous: FlameState | None,
        result: KeepResult,
        checked_at: datetime,
        target: BusinessTarget,
    ) -> FlameState:
        if result.status is KeepStatus.SUCCESS:
            last_success = result.receipt.observed_at
            failures = 0
            pending_since = None
            action = None
            last_reminder = None
        elif result.status is KeepStatus.FAILED:
            last_success = None if previous is None else previous.last_success_at
            failures = (previous.consecutive_failures if previous is not None and previous.last_checked_target == target else 0) + 1
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
            last_reminder = previous.last_reminder_at if same_action else None

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
            last_checked_target=target,
            last_success_receipt=(result.receipt if result.status is KeepStatus.SUCCESS else
                                  None if previous is None else previous.last_success_receipt),
        )

    @classmethod
    def _notification(
        cls,
        flame: FlameConfig,
        previous: FlameState | None,
        result: KeepResult,
        now: datetime,
        target: BusinessTarget,
    ) -> tuple[str | None, bool]:
        if result.status is KeepStatus.WAITING_HUMAN:
            action_changed = (
                previous is None
                or previous.status is not KeepStatus.WAITING_HUMAN
                or previous.human_action is None
                or result.human_action is None
                or previous.human_action.kind != result.human_action.kind
            )
            new_schedule_day = previous is None or previous.last_checked_target != target
            if action_changed or new_schedule_day:
                return "human_action_required", target.contains(now) and cls._at_or_after_reminder(flame, now)
            if target.contains(now) and reminder_due(flame.schedule, previous, now):
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
    def _at_or_after_reminder(flame: FlameConfig, now: datetime) -> bool:
        zone = ZoneInfo(flame.schedule.timezone)
        return now.astimezone(zone).time() >= flame.schedule.reminder_at
