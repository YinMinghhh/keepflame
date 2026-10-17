"""Per-day first-run draws and same-day ordering between Flames."""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from keepflame.core.config import AppConfig, FlameConfig
from keepflame.core.models import FlameState
from keepflame.core.schedule import draw_first_run, due_at, window_bounds


def window_label(flame: FlameConfig) -> str:
    """A draw is only reused for the window it was drawn in."""
    schedule = flame.schedule
    end = schedule.first_window_end
    return f"{schedule.timezone}|{schedule.first_check:%H:%M}-{'' if end is None else f'{end:%H:%M}'}"


class DailyPlanner:
    """Draws one uniform first-run time per Flame and business day.

    The draw is persisted through ``storage.daily_first_run`` so a restart never
    redraws it. Storages without that method (test doubles) get a stable,
    hash-seeded draw instead. ``randrange`` is injectable for deterministic tests.
    """

    def __init__(self, storage, *, randrange: Callable[[int], int] | None = None) -> None:
        self.storage = storage
        self.randrange = randrange or random.SystemRandom().randrange

    def first_run(self, flame: FlameConfig, day: date) -> datetime:
        schedule = flame.schedule
        if not schedule.randomized:
            return window_bounds(schedule, day)[0]
        start, end = window_bounds(schedule, day)
        label = window_label(flame)
        persist = getattr(self.storage, "daily_first_run", None)
        if callable(persist):
            value = persist(flame.id, flame.account_id, day.isoformat(), label,
                            lambda: draw_first_run(schedule, day, self.randrange))
        else:
            seed = hashlib.sha256(f"{flame.id}|{flame.account_id}|{day}|{label}".encode()).digest()
            value = draw_first_run(schedule, day, random.Random(seed).randrange)
        value = value.astimezone(timezone.utc)
        # A corrupt or foreign row never moves the run outside today's window.
        return value if start <= value < end else start

    def day_floor(self, config: AppConfig | None, flame: FlameConfig, day: date) -> datetime:
        floor = self.first_run(flame, day)
        anchor_id = flame.schedule.after_flame
        if anchor_id is None or config is None:
            return floor
        anchor = next((item for item in config.enabled_flames() if item.id == anchor_id), None)
        if anchor is None:
            return floor
        gap = timedelta(minutes=flame.schedule.min_gap_minutes)
        floor = max(floor, self.first_run(anchor, day) + gap)
        state = self.storage.get_state(anchor.id, anchor.account_id)
        zone = ZoneInfo(flame.schedule.timezone)
        if state is not None and state.last_checked_at.astimezone(zone).date() == day:
            # Never start within the gap after the anchor's latest run that day.
            floor = max(floor, state.last_checked_at.astimezone(timezone.utc) + gap)
        return floor

    def due(self, config: AppConfig | None, flame: FlameConfig, state: FlameState | None, now: datetime) -> datetime:
        return due_at(flame.schedule, state, now, day_floor=lambda day: self.day_floor(config, flame, day))
