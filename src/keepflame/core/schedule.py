"""Timezone-aware daily windows and persisted, bounded recheck scheduling."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from keepflame.core.config import ScheduleConfig
from keepflame.core.models import BusinessTarget, FlameState, KeepStatus


def _at(day: date, schedule_time, zone: ZoneInfo) -> datetime:
    # A UTC round trip moves a nonexistent DST wall time forward across the gap.
    return datetime.combine(day, schedule_time, tzinfo=zone).astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler requires timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def _in_window(schedule: ScheduleConfig, candidate: datetime) -> datetime:
    candidate = _as_utc(candidate)
    zone = ZoneInfo(schedule.timezone)
    day = candidate.astimezone(zone).date()
    first = _at(day, schedule.first_check, zone)
    stop = _at(day, schedule.stop_at, zone)
    if first >= stop:
        return _at(day + timedelta(days=1), schedule.first_check, zone)
    if candidate < first:
        return first
    if candidate >= stop:
        return _at(day + timedelta(days=1), schedule.first_check, zone)
    return candidate


def next_after_result(
    schedule: ScheduleConfig,
    now: datetime,
    status: KeepStatus,
    *,
    requested_at: datetime | None = None,
    consecutive_failures: int = 0,
    completed_target: BusinessTarget | None = None,
) -> datetime:
    now = _as_utc(now)
    zone = ZoneInfo(schedule.timezone)
    day = now.astimezone(zone).date()
    if status is KeepStatus.SUCCESS:
        if completed_target is not None:
            if completed_target.timezone != schedule.timezone:
                raise ValueError("completion timezone differs from schedule")
            day = date.fromisoformat(completed_target.day)
        candidate = max(now, _at(day + timedelta(days=1), schedule.first_check, zone))
    else:
        minutes = schedule.recheck_minutes
        if status is KeepStatus.FAILED:
            factor = 2 ** min(max(consecutive_failures - 1, 0), 10)
            minutes = min(minutes * factor, max(minutes, 360))
        candidate = now + timedelta(minutes=minutes)
    # Platform cooldowns are lower bounds, never permission for an earlier retry.
    if requested_at is not None:
        candidate = max(candidate, _as_utc(requested_at))
    return _in_window(schedule, candidate)


def due_at(
    schedule: ScheduleConfig,
    state: FlameState | None,
    now: datetime,
) -> datetime:
    now = _as_utc(now)
    candidate = now
    zone = ZoneInfo(schedule.timezone)
    today = now.astimezone(zone).date()
    if state is not None:
        if state.next_action_at is not None:
            candidate = max(candidate, _as_utc(state.next_action_at))
        if state.last_success_receipt is not None:
            target = state.last_success_receipt.target
            if target.timezone != schedule.timezone:
                raise ValueError("persisted completion timezone differs from schedule")
            completed = date.fromisoformat(target.day)
            candidate = max(candidate, _at(completed + timedelta(days=1), schedule.first_check, zone))
        elif (
            state.status is KeepStatus.SUCCESS
            and state.last_success_at is not None
            and state.last_success_at.astimezone(zone).date() == today
        ):
            candidate = max(candidate, _at(today + timedelta(days=1), schedule.first_check, zone))
    # Preserve future days as well as today's schedule, including after restart.
    return _in_window(schedule, candidate)


def reminder_due(schedule: ScheduleConfig, state: FlameState | None, now: datetime) -> bool:
    if state is None or state.status is not KeepStatus.WAITING_HUMAN:
        return False
    now = _as_utc(now)
    zone = ZoneInfo(schedule.timezone)
    today = now.astimezone(zone).date()
    if not _at(today, schedule.reminder_at, zone) <= now < _at(today, schedule.stop_at, zone):
        return False
    return state.last_reminder_at is None or state.last_reminder_at.astimezone(zone).date() != today
