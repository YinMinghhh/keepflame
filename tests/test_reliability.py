"""Regression boundaries use synthetic clocks, transports and isolated SQLite."""

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import sqlite3

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.config import AppConfig, ScheduleConfig, StorageConfig
from keepflame.core.models import KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.schedule import due_at, next_after_result
from keepflame.core.service import SchedulerService
from keepflame.storage import SQLiteStorage
from keepflame.storage.sqlite import StorageError
from tests.helpers import FakeNotifier, FixedClock, SequenceKeeper, StaticRegistry, StaticSecrets
from tests.test_orchestrator import flame, local, wait_result


def setup(tmp_path, results, *, notifier=None, current=None, storage=None):
    storage = storage or SQLiteStorage(tmp_path / "state.sqlite3")
    keeper = SequenceKeeper(*results)
    clock = FixedClock(current or local(11, 9))
    notifier = notifier or FakeNotifier()
    runner = Orchestrator(
        storage=storage, registry=StaticRegistry(keeper), clock=clock,
        capabilities=CapabilityRegistry({}), secrets=StaticSecrets("unused=synthetic"),
        notifier=notifier,
    )
    config = AppConfig(StorageConfig(storage.path), (flame(),), tmp_path / "fake.toml")
    service = SchedulerService(config=config, storage=storage, orchestrator=runner, clock=clock)
    return runner, service, storage, keeper, clock, notifier


@pytest.mark.parametrize("result", [wait_result(), KeepResult(KeepStatus.FAILED, "offline")])
def test_late_window_does_not_spin_and_restart_preserves_next_day(tmp_path, result):
    runner, service, storage, keeper, clock, _ = setup(tmp_path, [result], current=local(11, 22))
    outcomes, next_due = service.run_due()
    assert len(outcomes) == 1
    assert next_due == local(12, 9)
    for instant in [local(11, 22) + timedelta(milliseconds=250), local(11, 23), local(12, 8)]:
        clock.current = instant
        assert service.run_due() == ([], local(12, 9))
    assert storage.count_runs() == 1
    reopened = SQLiteStorage(storage.path)
    _, restarted, reopened, _, restarted_clock, _ = setup(
        tmp_path, [KeepResult(KeepStatus.SUCCESS, "done")], storage=reopened, current=local(12, 8),
    )
    assert restarted.run_due() == ([], local(12, 9))
    restarted_clock.current = local(12, 9)
    assert len(restarted.run_due()[0]) == 1
    assert reopened.count_runs() == 2


def test_success_is_idempotent_for_day_and_unattended_waiting_resumes_next_day(tmp_path):
    runner, service, storage, keeper, clock, notifier = setup(
        tmp_path, [KeepResult(KeepStatus.SUCCESS, "done"), wait_result(), wait_result()],
    )
    assert len(service.run_due()[0]) == 1
    for instant in (local(11, 10), local(11, 22), local(12, 8)):
        clock.current = instant
        assert service.run_due()[0] == []
    clock.current = local(12, 9)
    assert len(service.run_due()[0]) == 1
    clock.current = local(12, 10)
    assert len(service.run_due()[0]) == 1
    assert storage.count_runs() == 3
    assert [e["event"] for e in notifier.events] == ["human_action_required"]


def test_failure_backoff_is_bounded_and_success_resets_it(tmp_path):
    schedule = flame().schedule
    delays = [
        next_after_result(schedule, local(11, 9), KeepStatus.FAILED, consecutive_failures=n)
        - local(11, 9) for n in (1, 2, 3, 4, 1000000)
    ]
    assert delays == [timedelta(hours=h) for h in (1, 2, 4, 6, 6)]
    runner, _, storage, _, _, _ = setup(tmp_path, [
        KeepResult(KeepStatus.FAILED, "offline"), KeepResult(KeepStatus.SUCCESS, "done"),
        KeepResult(KeepStatus.FAILED, "offline"),
    ])
    runner.clock.current = local(11, 9)
    runner.run_flame(flame(), now=local(11, 9))
    runner.clock.current = local(11, 10)
    runner.run_flame(flame(), now=local(11, 10))
    runner.clock.current = local(12, 9)
    result = runner.run_flame(flame(), now=local(12, 9))
    assert result.state.consecutive_failures == 1
    assert result.state.next_action_at == local(12, 10)


def test_platform_cooldown_is_preserved_and_clamped_to_window(tmp_path):
    result = replace(wait_result(), next_action_at=local(13, 2))
    runner, service, storage, _, clock, _ = setup(tmp_path, [result])
    runner.clock.current = local(11, 9)
    first = runner.run_flame(flame(), now=local(11, 9))
    assert first.state.next_action_at == local(13, 9)
    clock.current = local(12, 10)
    assert service.run_due() == ([], local(13, 9))
    assert next_after_result(flame().schedule, local(11, 9), KeepStatus.FAILED,
                             requested_at=local(10, 9)) == local(11, 10)


def test_restart_catches_up_once_after_downtime(tmp_path):
    runner, _, storage, _, _, _ = setup(tmp_path, [wait_result()])
    runner.clock.current = local(11, 9)
    runner.run_flame(flame(), now=local(11, 9))
    _, service, _, _, _, _ = setup(tmp_path, [wait_result()], storage=SQLiteStorage(storage.path), current=local(12, 11))
    assert len(service.run_due()[0]) == 1
    assert service.run_due() == ([], local(12, 12))


@pytest.mark.parametrize("day,hours", [(7, 23), (31, 25)])
def test_dst_day_length_is_derived_from_local_calendar(day, hours):
    from zoneinfo import ZoneInfo
    month = 3 if day == 7 else 10
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, month, day, 9, tzinfo=zone).astimezone(timezone.utc)
    schedule = ScheduleConfig("America/New_York", time(9), 60, time(20), time(23))
    assert next_after_result(schedule, now, KeepStatus.SUCCESS) - now == timedelta(hours=hours)


def test_dst_gap_never_returns_an_already_closed_window():
    schedule = ScheduleConfig("America/New_York", time(2, 30), 60, time(3), time(3, 15))
    now = datetime(2026, 3, 8, 6, tzinfo=timezone.utc)
    assert due_at(schedule, None, now) == datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc)


class FlakyNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.fail = True
        self.calls = 0

    def notify(self, event, **fields):
        self.calls += 1
        if self.fail:
            raise OSError("synthetic delivery failure with data that must not be persisted")
        super().notify(event, **fields)


def test_notification_retries_after_restart_without_new_remote_check(tmp_path):
    notifier = FlakyNotifier()
    runner, service, storage, _, clock, _ = setup(tmp_path, [wait_result()], notifier=notifier, current=local(11, 20))
    service.run_due()
    pending = storage.get_notification(flame().id, flame().account_id)
    assert pending.attempts == 1
    assert pending.last_error_type == "OSError"
    assert storage.get_state(flame().id, flame().account_id).last_reminder_at is None
    _, restarted, storage, keeper, clock, _ = setup(
        tmp_path, [], storage=SQLiteStorage(storage.path), notifier=notifier,
        current=local(11, 20) + timedelta(seconds=59),
    )
    assert restarted.run_due()[0] == []
    assert notifier.calls == 1
    notifier.fail = False
    clock.current += timedelta(seconds=1)
    assert restarted.run_due()[0] == []
    assert storage.count_runs() == 1
    assert storage.get_notification(flame().id, flame().account_id) is None
    assert storage.get_state(flame().id, flame().account_id).last_reminder_at == local(11, 20)
    assert notifier.events[0]["notification_id"] == pending.notification_id


def test_notification_retry_budget_does_not_reset_on_same_status(tmp_path):
    notifier = FlakyNotifier()
    runner, service, storage, _, clock, _ = setup(tmp_path, [wait_result(), wait_result()], notifier=notifier)
    service.run_due()
    for minute in (1, 3, 7, 15):
        clock.current = local(11, 9) + timedelta(minutes=minute)
        service.run_due()
    assert notifier.calls == 5
    pending = storage.get_notification(flame().id, flame().account_id)
    assert pending.retry_at is None and pending.attempts == 5
    clock.current = local(11, 10)
    service.run_due()
    assert notifier.calls == 5
    assert storage.get_notification(flame().id, flame().account_id).notification_id == pending.notification_id


def test_queued_notification_from_run_once_is_not_lost(tmp_path):
    runner, service, storage, _, clock, notifier = setup(tmp_path, [wait_result()])
    runner.run_flame(flame(), now=clock.current, notify=False)
    assert notifier.events == []
    assert storage.get_notification(flame().id, flame().account_id) is not None
    clock.current += timedelta(minutes=1)
    assert service.run_due()[0] == []
    assert [e["event"] for e in notifier.events] == ["human_action_required"]
    assert storage.count_runs() == 1


def test_recovery_replaces_stale_human_action(tmp_path):
    runner, service, storage, _, clock, notifier = setup(tmp_path, [wait_result(), KeepResult(KeepStatus.SUCCESS, "done")])
    runner.run_flame(flame(), now=clock.current, notify=False)
    clock.current += timedelta(hours=1)
    service.run_due()
    assert [e["event"] for e in notifier.events] == ["flame_recovered"]


def test_disabled_flame_does_not_execute_or_deliver(tmp_path):
    runner, service, storage, keeper, clock, notifier = setup(tmp_path, [wait_result()])
    runner.run_flame(flame(), now=clock.current, notify=False)
    service.config = replace(service.config, flames=(replace(flame(), enabled=False),))
    clock.current += timedelta(days=1)
    assert service.run_due() == ([], None)
    assert notifier.events == [] and storage.count_runs() == 1


def test_second_runner_is_rejected_and_status_stays_readable(tmp_path):
    first = SQLiteStorage(tmp_path / "state.sqlite3")
    second = SQLiteStorage(first.path)
    with first.execution_lock():
        with pytest.raises(StorageError, match="already using"):
            with second.execution_lock():
                pytest.fail("a second runner acquired the same database")
        second.healthcheck()
    with second.execution_lock():
        second.healthcheck()


def test_schema_v1_is_migrated_without_losing_history(tmp_path):
    runner, _, storage, _, clock, _ = setup(tmp_path, [KeepResult(KeepStatus.SUCCESS, "done")])
    runner.run_flame(flame(), now=clock.current)
    with sqlite3.connect(storage.path) as db:
        db.execute("DROP TABLE pending_notifications")
        db.execute("UPDATE schema_meta SET version=1")
    reopened = SQLiteStorage(storage.path)
    assert reopened.count_runs() == 1
    assert reopened.get_state(flame().id, flame().account_id).status is KeepStatus.SUCCESS
    assert reopened.get_notification(flame().id, flame().account_id) is None
    reopened.healthcheck()


def test_pending_failure_alert_is_retained_across_days(tmp_path):
    failure = KeepResult(KeepStatus.FAILED, "offline")
    runner, _, storage, _, _, _ = setup(tmp_path, [failure, failure])
    runner.clock.current = local(11, 9)
    runner.run_flame(flame(), now=local(11, 9), notify=False)
    first = storage.get_notification(flame().id, flame().account_id)
    runner.clock.current = local(12, 9)
    runner.run_flame(flame(), now=local(12, 9), notify=False)
    assert storage.get_notification(flame().id, flame().account_id).notification_id == first.notification_id


def test_notification_can_retry_outside_remote_check_window(tmp_path):
    notifier = FlakyNotifier()
    _, service, storage, _, clock, _ = setup(
        tmp_path, [wait_result()], notifier=notifier,
        current=local(11, 23) - timedelta(minutes=1),
    )
    service.run_due()
    notifier.fail = False
    clock.current = local(11, 23)
    assert service.run_due() == ([], local(12, 9))
    assert storage.count_runs() == 1
    assert len(notifier.events) == 1


def test_notification_and_state_rollback_together(tmp_path):
    runner, _, storage, _, clock, _ = setup(tmp_path, [wait_result()])
    outcome = runner.run_flame(flame(), now=clock.current, notify=False)
    original = storage.get_notification(flame().id, flame().account_id)
    changed = replace(original, notification_id="must-not-be-saved")
    with pytest.raises(StorageError, match="failed to record"):
        storage.record(outcome.record, replace(outcome.state, message="must-not-win"), changed)
    assert storage.get_notification(flame().id, flame().account_id) == original
    assert storage.get_state(flame().id, flame().account_id) == outcome.state


def test_corrupt_state_json_skips_dispatch_without_killing_the_tick(tmp_path):
    import sqlite3
    import threading

    runner, service, storage, keeper, clock, notifier = setup(
        tmp_path, [KeepResult(KeepStatus.SUCCESS, "done")],
    )
    runner.run_flame(flame(), now=local(11, 9))
    with sqlite3.connect(storage.path) as db:
        db.execute(
            "UPDATE flame_state SET metadata_json = ? WHERE flame_id = ? AND account_id = ?",
            ("NOT-JSON{", flame().id, flame().account_id),
        )
    keeper.results.append(KeepResult(KeepStatus.SUCCESS, "should-not-run"))
    clock.current = local(12, 9)
    outcomes, next_due = service.run_due(now=local(12, 9))
    assert outcomes == []
    assert [event["event"] for event in notifier.events] == ["storage_corrupt"]
    assert notifier.events[0]["result"].metadata["reason"] == "storage_corrupt"
    # Same business day: alert is not repeated.
    assert service.run_due(now=local(12, 10))[0] == []
    assert len(notifier.events) == 1


def test_run_forever_survives_corrupt_state_and_can_be_stopped(tmp_path):
    import sqlite3
    import threading

    runner, service, storage, keeper, clock, notifier = setup(
        tmp_path, [KeepResult(KeepStatus.SUCCESS, "done")],
    )
    runner.run_flame(flame(), now=local(11, 9))
    with sqlite3.connect(storage.path) as db:
        db.execute(
            "UPDATE flame_state SET metadata_json = ? WHERE flame_id = ? AND account_id = ?",
            ("NOT-JSON{", flame().id, flame().account_id),
        )
    stop = threading.Event()
    service.stop_event = stop
    clock.current = local(12, 9)

    def halt_soon():
        import time
        time.sleep(0.2)
        stop.set()

    threading.Thread(target=halt_soon, daemon=True).start()
    service.run_forever()  # must return after stop; must not raise
    assert any(event["event"] == "storage_corrupt" for event in notifier.events)
