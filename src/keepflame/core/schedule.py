"""Daily inspection-window scheduling without becoming a cron manager."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from keepflame.core.config import ScheduleConfig
from keepflame.core.models import FlameState, KeepStatus


def _at(day: date, schedule_time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, schedule_time, tzinfo=zone)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler requires timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def next_after_result(
    schedule: ScheduleConfig,
    now: datetime,
    status: KeepStatus,
) -> datetime:
    zone = ZoneInfo(schedule.timezone)
    local_now = _as_utc(now).astimezone(zone)
    today = local_now.date()
    if status is KeepStatus.SUCCESS:
        return _at(today + timedelta(days=1), schedule.first_check, zone).astimezone(
            timezone.utc
        )

    stop = _at(today, schedule.stop_at, zone)
    candidate = local_now + timedelta(minutes=schedule.recheck_minutes)
    if local_now >= stop or candidate >= stop:
        return _at(today + timedelta(days=1), schedule.first_check, zone).astimezone(
            timezone.utc
        )
    return candidate.astimezone(timezone.utc)


def due_at(
    schedule: ScheduleConfig,
    state: FlameState | None,
    now: datetime,
) -> datetime:
    zone = ZoneInfo(schedule.timezone)
    now_utc = _as_utc(now)
    local_now = now_utc.astimezone(zone)
    today = local_now.date()
    first = _at(today, schedule.first_check, zone)
    stop = _at(today, schedule.stop_at, zone)

    if local_now < first:
        return first.astimezone(timezone.utc)
    if local_now >= stop:
        return _at(today + timedelta(days=1), schedule.first_check, zone).astimezone(
            timezone.utc
        )
    if (
        state is not None
        and state.status is KeepStatus.SUCCESS
        and state.last_success_at is not None
        and state.last_success_at.astimezone(zone).date() == today
    ):
        return _at(today + timedelta(days=1), schedule.first_check, zone).astimezone(
            timezone.utc
        )
    if state is not None and state.next_action_at is not None:
        stored = state.next_action_at.astimezone(timezone.utc)
        stored_local = stored.astimezone(zone)
        if now_utc < stored and stored_local.date() == today and stored_local < stop:
            return stored
    return now_utc


def reminder_due(
    schedule: ScheduleConfig,
    state: FlameState | None,
    now: datetime,
) -> bool:
    if state is None or state.status is not KeepStatus.WAITING_HUMAN:
        return False
    zone = ZoneInfo(schedule.timezone)
    local_now = _as_utc(now).astimezone(zone)
    reminder = _at(local_now.date(), schedule.reminder_at, zone)
    stop = _at(local_now.date(), schedule.stop_at, zone)
    if local_now < reminder or local_now >= stop:
        return False
    if state.last_reminder_at is None:
        return True
    return state.last_reminder_at.astimezone(zone).date() != local_now.date()

