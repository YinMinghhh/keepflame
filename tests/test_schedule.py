from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from keepflame.core.config import ScheduleConfig
from keepflame.core.models import FlameState, KeepStatus
from keepflame.core.schedule import due_at, next_after_result, reminder_due

ZONE = ZoneInfo("Asia/Shanghai")
SCHEDULE = ScheduleConfig(
    timezone="Asia/Shanghai",
    first_check=time(9, 0),
    recheck_minutes=60,
    reminder_at=time(20, 0),
    stop_at=time(23, 0),
)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=ZONE).astimezone(timezone.utc)


def state(status: KeepStatus, checked: datetime, **overrides) -> FlameState:
    values = {
        "flame_id": "joinquant-primary",
        "account_id": "primary",
        "status": status,
        "message": "state",
        "last_checked_at": checked,
        "last_success_at": checked if status is KeepStatus.SUCCESS else None,
        "consecutive_failures": 0,
        "pending_human_since": checked if status is KeepStatus.WAITING_HUMAN else None,
        "human_action": None,
        "next_action_at": None,
        "last_reminder_at": None,
        "metadata": {},
    }
    values.update(overrides)
    return FlameState(**values)


def test_initial_due_respects_daily_window() -> None:
    assert due_at(SCHEDULE, None, local(11, 8)) == local(11, 9)
    assert due_at(SCHEDULE, None, local(11, 10)) == local(11, 10)
    assert due_at(SCHEDULE, None, local(11, 23)) == local(12, 9)


def test_success_stops_checks_for_the_day() -> None:
    current = state(KeepStatus.SUCCESS, local(11, 9))
    assert due_at(SCHEDULE, current, local(11, 12)) == local(12, 9)


def test_waiting_rechecks_hourly_until_stop() -> None:
    assert next_after_result(SCHEDULE, local(11, 10), KeepStatus.WAITING_HUMAN) == local(11, 11)
    assert next_after_result(SCHEDULE, local(11, 22, 30), KeepStatus.WAITING_HUMAN) == local(12, 9)


def test_reminder_is_once_per_local_day() -> None:
    waiting = state(KeepStatus.WAITING_HUMAN, local(11, 9))
    assert not reminder_due(SCHEDULE, waiting, local(11, 19, 59))
    assert reminder_due(SCHEDULE, waiting, local(11, 20))
    reminded = state(
        KeepStatus.WAITING_HUMAN,
        local(11, 20),
        last_reminder_at=local(11, 20),
    )
    assert not reminder_due(SCHEDULE, reminded, local(11, 21))
    assert reminder_due(SCHEDULE, reminded, local(12, 20))

