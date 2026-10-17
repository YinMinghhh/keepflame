"""Timezone-aware daily windows and persisted, bounded recheck scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
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


RETRIES_EXHAUSTED_KEY = "retries_exhausted_day"


@dataclass(frozen=True, slots=True)
class FailurePlan:
    """When a FAILED run may try again; ``exhausted`` means not again this business day."""

    next_at: datetime
    exhausted: bool


def window_bounds(schedule: ScheduleConfig, day: date) -> tuple[datetime, datetime]:
    """UTC bounds of the first-run window; a fixed first_check yields a zero-width window."""
    zone = ZoneInfo(schedule.timezone)
    start = _at(day, schedule.first_check, zone)
    end = _at(day, schedule.first_window_end, zone) if schedule.first_window_end is not None else start
    return start, max(start, end)


def draw_first_run(schedule: ScheduleConfig, day: date, randrange: Callable[[int], int]) -> datetime:
    """One uniform second in [window start, window end); fixed schedules return first_check."""
    start, end = window_bounds(schedule, day)
    span = int((end - start).total_seconds())
    if span <= 0:
        return start
    offset = randrange(span)
    if type(offset) is not int or not 0 <= offset < span:
        raise ValueError("random draw outside the first-run window")
    return start + timedelta(seconds=offset)


def failure_plan(
    schedule: ScheduleConfig,
    now: datetime,
    failures: int,
    *,
    requested_at: datetime | None = None,
) -> FailurePlan:
    """Bounded same-day retries: retry_minutes[n-1] after the n-th failure, then stop."""
    now = _as_utc(now)
    zone = ZoneInfo(schedule.timezone)
    day = now.astimezone(zone).date()
    tomorrow = max(_at(day + timedelta(days=1), schedule.first_check, zone), now)
    floor = None if requested_at is None else _as_utc(requested_at)
    # Platform cooldowns are lower bounds, never permission for an earlier retry,
    # and they survive into later days when today's retries are used up.
    stopped = _in_window(schedule, tomorrow if floor is None else max(tomorrow, floor))
    if not 1 <= failures <= len(schedule.retry_minutes):
        return FailurePlan(stopped, True)
    candidate = now + timedelta(minutes=schedule.retry_minutes[failures - 1])
    if floor is not None:
        candidate = max(candidate, floor)
    if candidate >= _at(day, schedule.stop_at, zone) or candidate.astimezone(zone).date() != day:
        return FailurePlan(stopped, True)
    return FailurePlan(_in_window(schedule, candidate), False)


def next_after_result(
    schedule: ScheduleConfig,
    now: datetime,
    status: KeepStatus,
    *,
    requested_at: datetime | None = None,
    consecutive_failures: int = 0,
    completed_target: BusinessTarget | None = None,
    maximum: timedelta | None = None,
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
    elif status is KeepStatus.FAILED:
        return failure_plan(schedule, now, max(consecutive_failures, 1), requested_at=requested_at).next_at
    else:
        candidate = now + timedelta(minutes=schedule.recheck_minutes)
    # Platform cooldowns are lower bounds, never permission for an earlier retry.
    if requested_at is not None:
        candidate = max(candidate, _as_utc(requested_at))
    if maximum is not None and candidate - now >= maximum:
        candidate = now + maximum - timedelta(seconds=1)
    return _in_window(schedule, candidate)


def due_at(
    schedule: ScheduleConfig,
    state: FlameState | None,
    now: datetime,
    *,
    day_floor: Callable[[date], datetime | None] | None = None,
) -> datetime:
    """Earliest admissible run. ``day_floor(day)`` adds that day's drawn/staggered start."""
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
        exhausted = state.metadata.get(RETRIES_EXHAUSTED_KEY)
        if isinstance(exhausted, str) and exhausted == today.isoformat():
            candidate = max(candidate, _at(today + timedelta(days=1), schedule.first_check, zone))
    # Preserve future days as well as today's schedule, including after restart.
    candidate = _in_window(schedule, candidate)
    if day_floor is None:
        return candidate
    for _ in range(4):
        floor = day_floor(candidate.astimezone(zone).date())
        if floor is None or candidate >= _as_utc(floor):
            return candidate
        candidate = _in_window(schedule, _as_utc(floor))
    return candidate


def reminder_due(schedule: ScheduleConfig, state: FlameState | None, now: datetime) -> bool:
    if state is None or state.status is not KeepStatus.WAITING_HUMAN:
        return False
    now = _as_utc(now)
    zone = ZoneInfo(schedule.timezone)
    today = now.astimezone(zone).date()
    if not _at(today, schedule.reminder_at, zone) <= now < _at(today, schedule.stop_at, zone):
        return False
    return state.last_reminder_at is None or state.last_reminder_at.astimezone(zone).date() != today
