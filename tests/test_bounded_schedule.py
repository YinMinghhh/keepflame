"""Random daily start, same-day staggering and bounded FAILED retries (synthetic clocks only)."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.config import AppConfig, ConfigError, FlameConfig, ScheduleConfig, StorageConfig, load_config
from keepflame.core.models import KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.planner import DailyPlanner
from keepflame.core.schedule import (
    RETRIES_EXHAUSTED_KEY, draw_first_run, due_at, failure_plan, next_after_result,
)
from keepflame.core.service import SchedulerService
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper
from keepflame.storage import SQLiteStorage
from tests.helpers import FakeNotifier, FixedClock, MemoryStorage, SequenceKeeper, StaticRegistry, StaticSecrets
from tests.test_orchestrator import wait_result

ZONE = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 8, 11)


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=ZONE).astimezone(timezone.utc)


def schedule(**overrides) -> ScheduleConfig:
    values = dict(timezone="Asia/Shanghai", first_check=time(9), recheck_minutes=60,
                  reminder_at=time(20), stop_at=time(23), first_window_end=time(11))
    values.update(overrides)
    return ScheduleConfig(**values)


def flame(flame_id="joinquant-primary", **overrides) -> FlameConfig:
    return FlameConfig(id=flame_id, keeper="fake", account_id="primary", enabled=True,
                       schedule=schedule(**overrides), settings={})


class Draws:
    """Deterministic randrange: queued offsets (then 0) and a record of each span."""

    def __init__(self, *offsets):
        self.offsets = list(offsets)
        self.spans = []

    def __call__(self, span):
        self.spans.append(span)
        return self.offsets.pop(0) if self.offsets else 0


# --- configuration -----------------------------------------------------------------

BASE = """
[storage]
path = "state.sqlite3"

[[flames]]
id = "joinquant-primary"
keeper = "joinquant"
account_id = "primary"
enabled = true

[flames.schedule]
timezone = "Asia/Shanghai"
{checkin}
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"

[flames.settings]
task = "checkin"

[[flames]]
id = "joinquant-reading-primary"
keeper = "joinquant"
account_id = "primary"
enabled = true

[flames.schedule]
timezone = "{reading_zone}"
{reading}
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"

[flames.settings]
task = "reading"
"""


def load(tmp_path: Path, checkin="", reading="", reading_zone="Asia/Shanghai"):
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(checkin=checkin, reading=reading, reading_zone=reading_zone))
    return load_config(path)


def test_defaults_are_random_window_nine_to_eleven_with_thirty_sixty_one_twenty_retries(tmp_path):
    config = load(tmp_path)
    checkin = config.flames[0].schedule
    assert checkin.first_check == time(9) and checkin.first_window_end == time(11)
    assert checkin.randomized and checkin.retry_minutes == (30, 60, 120)
    assert checkin.after_flame is None and checkin.min_gap_minutes == 5


def test_first_check_alone_keeps_the_fixed_daily_start(tmp_path):
    config = load(tmp_path, checkin='first_check = "09:00"', reading='first_check = "09:10"')
    assert config.flames[0].schedule.first_window_end is None
    assert not config.flames[1].schedule.randomized


def test_window_ordering_and_retry_keys_load(tmp_path):
    config = load(
        tmp_path,
        checkin='first_window_start = "09:00"\nfirst_window_end = "11:00"\nretry_minutes = [15, 45]',
        reading=('first_window_start = "09:30"\nfirst_window_end = "11:30"\n'
                 'after_flame = "joinquant-primary"\nmin_gap_minutes = 7'),
    )
    checkin, reading = (item.schedule for item in config.flames)
    assert checkin.retry_minutes == (15, 45)
    assert (reading.first_check, reading.first_window_end) == (time(9, 30), time(11, 30))
    assert reading.after_flame == "joinquant-primary" and reading.min_gap_minutes == 7


@pytest.mark.parametrize("checkin, reading, message", [
    ('first_check = "09:00"\nfirst_window_end = "11:00"', "", "either first_check"),
    ('first_window_start = "09:00"', "", "set together"),
    ('first_window_start = "11:00"\nfirst_window_end = "09:00"', "", "before first_window_end"),
    ('first_window_start = "09:00"\nfirst_window_end = "09:00"', "", "before first_window_end"),
    ('first_window_start = "09:00"\nfirst_window_end = "23:00"', "", "first_window_end < stop_at"),
    ('first_window_start = "21:00"\nfirst_window_end = "22:00"', "", "first_check < reminder_at"),
    ("retry_minutes = []", "", "retry_minutes"),
    ("retry_minutes = [1,1,1,1,1,1,1,1,1,1,1]", "", "retry_minutes"),
    ("retry_minutes = [0]", "", "retry_minutes"),
    ("retry_minutes = [-5]", "", "retry_minutes"),
    ("retry_minutes = [true]", "", "retry_minutes"),
    ("retry_minutes = [721]", "", "retry_minutes"),
    ("retry_minutes = 30", "", "retry_minutes"),
    ("", "min_gap_minutes = 5", "requires after_flame"),
    ("", 'after_flame = "missing"', "unknown flame"),
    ("", 'after_flame = "joinquant-reading-primary"', "cannot name itself"),
    ('after_flame = "joinquant-reading-primary"', 'after_flame = "joinquant-primary"', "cannot itself use"),
    ("", 'after_flame = "joinquant-primary"\nmin_gap_minutes = -1', "min_gap_minutes"),
    ("", 'after_flame = "joinquant-primary"\nmin_gap_minutes = 721', "min_gap_minutes"),
    ("", "bogus_key = 1", "unknown"),
])
def test_invalid_schedule_configuration_is_rejected(tmp_path, checkin, reading, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, checkin=checkin, reading=reading)


def test_after_flame_must_share_the_timezone(tmp_path):
    with pytest.raises(ConfigError, match="same timezone"):
        load(tmp_path, reading='after_flame = "joinquant-primary"', reading_zone="Asia/Tokyo")


# --- daily draw --------------------------------------------------------------------

def test_draw_is_uniform_over_the_window_in_seconds():
    draws = Draws(0, 7199)
    assert draw_first_run(schedule(), DAY, draws) == at(11, 9)
    assert draw_first_run(schedule(), DAY, draws) == at(11, 10, 59, 59)
    assert draws.spans == [7200, 7200]
    with pytest.raises(ValueError):
        draw_first_run(schedule(), DAY, lambda span: span)
    assert draw_first_run(schedule(first_window_end=None), DAY, Draws()) == at(11, 9)


def test_draw_is_persisted_per_day_and_survives_restart(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    draws = Draws(1800, 3600)
    planner = DailyPlanner(store, randrange=draws)
    checkin = flame()
    assert planner.first_run(checkin, DAY) == at(11, 9, 30)
    assert planner.first_run(checkin, DAY) == at(11, 9, 30)
    restarted = DailyPlanner(SQLiteStorage(store.path), randrange=Draws())
    assert restarted.first_run(checkin, DAY) == at(11, 9, 30)
    # The next day draws again.
    assert planner.first_run(checkin, date(2026, 8, 12)) == at(12, 10)
    assert len(draws.spans) == 2


def test_changed_window_redraws_and_fixed_schedule_never_draws(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    planner = DailyPlanner(store, randrange=Draws(60, 120))
    assert planner.first_run(flame(), DAY) == at(11, 9, 1)
    assert planner.first_run(flame(first_check=time(10), first_window_end=time(12)), DAY) == at(11, 10, 2)
    assert DailyPlanner(store, randrange=Draws()).first_run(flame(first_window_end=None), DAY) == at(11, 9)


def test_storage_without_persistence_gets_a_stable_seeded_draw():
    first = DailyPlanner(MemoryStorage()).first_run(flame(), DAY)
    again = DailyPlanner(MemoryStorage()).first_run(flame(), DAY)
    assert first == again and at(11, 9) <= first < at(11, 11)


def test_corrupt_persisted_draw_never_leaves_the_window():
    class Store(MemoryStorage):
        def daily_first_run(self, *_args):
            return at(11, 3)
    assert DailyPlanner(Store()).first_run(flame(), DAY) == at(11, 9)


def test_due_waits_for_the_draw_and_success_moves_to_the_next_days_draw(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    planner = DailyPlanner(store, randrange=Draws(3600, 600))
    checkin = flame()
    assert planner.due(None, checkin, None, at(11, 8)) == at(11, 10)
    assert planner.due(None, checkin, None, at(11, 9, 59)) == at(11, 10)
    assert planner.due(None, checkin, None, at(11, 10, 5)) == at(11, 10, 5)
    success = SimpleNamespace(
        next_action_at=next_after_result(checkin.schedule, at(11, 10, 5), KeepStatus.SUCCESS),
        last_success_receipt=None, status=KeepStatus.SUCCESS, last_success_at=at(11, 10, 5), metadata={},
    )
    assert planner.due(None, checkin, success, at(11, 12)) == at(12, 9, 10)


# --- staggering --------------------------------------------------------------------

def pair(gap=5):
    checkin = flame()
    reading = flame("joinquant-reading-primary", after_flame="joinquant-primary", min_gap_minutes=gap)
    config = AppConfig(StorageConfig(Path("/synthetic")), (checkin, reading), Path("/synthetic.toml"))
    return config, checkin, reading


def test_reading_runs_at_least_the_gap_after_checkins_draw(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    config, checkin, reading = pair()
    # Check-in draws 10:30, reading independently draws 09:10.
    planner = DailyPlanner(store, randrange=Draws(5400, 600))
    assert planner.first_run(checkin, DAY) == at(11, 10, 30)
    assert planner.due(config, reading, None, at(11, 9, 15)) == at(11, 10, 35)


def test_reading_keeps_its_own_later_draw(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    config, checkin, reading = pair()
    # Reading draws first (10:40); check-in draws 09:00.
    planner = DailyPlanner(store, randrange=Draws(6000, 0))
    assert planner.due(config, reading, None, at(11, 9)) == at(11, 10, 40)


def test_reading_waits_the_gap_after_a_late_checkin_run(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    config, checkin, reading = pair(gap=10)
    planner = DailyPlanner(store, randrange=Draws(0, 0))
    runner = Orchestrator(storage=store, registry=StaticRegistry(SequenceKeeper(wait_result())),
                          capabilities=CapabilityRegistry({}), secrets=StaticSecrets("unused=synthetic"),
                          notifier=FakeNotifier(), clock=FixedClock(at(11, 12)))
    runner.run_flame(checkin, now=at(11, 12), notify=False)
    assert planner.due(config, reading, None, at(11, 12, 1)) == at(11, 12, 10)


def test_disabled_anchor_is_ignored(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    config, checkin, reading = pair()
    config = replace(config, flames=(replace(checkin, enabled=False), reading))
    planner = DailyPlanner(store, randrange=Draws(600))
    assert planner.due(config, reading, None, at(11, 9)) == at(11, 9, 10)


# --- bounded retries ---------------------------------------------------------------

def test_failures_retry_thirty_sixty_one_twenty_then_stop_for_the_day():
    plan = [failure_plan(schedule(), at(11, 10), n) for n in (1, 2, 3, 4)]
    assert [item.next_at for item in plan[:3]] == [at(11, 10, 30), at(11, 11), at(11, 12)]
    assert not any(item.exhausted for item in plan[:3])
    assert plan[3].exhausted and plan[3].next_at == at(12, 9)
    assert next_after_result(schedule(), at(11, 10), KeepStatus.FAILED, consecutive_failures=2) == at(11, 11)


def test_retry_past_stop_at_counts_as_exhausted():
    plan = failure_plan(schedule(), at(11, 22, 45), 1)
    assert plan.exhausted and plan.next_at == at(12, 9)


def test_retry_after_raises_the_floor_and_survives_exhaustion():
    assert failure_plan(schedule(), at(11, 10), 1, requested_at=at(11, 13)).next_at == at(11, 13)
    assert failure_plan(schedule(), at(11, 10), 1, requested_at=at(11, 9)).next_at == at(11, 10, 30)
    late = failure_plan(schedule(), at(11, 10), 1, requested_at=at(13, 2))
    assert late.exhausted and late.next_at == at(13, 9)


def scheduler(tmp_path, results, *, current, notifier=None, draws=None, flames=None):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    clock = FixedClock(current)
    notifier = notifier or FakeNotifier()
    keeper = SequenceKeeper(*results)
    runner = Orchestrator(storage=store, registry=StaticRegistry(keeper), clock=clock,
                          capabilities=CapabilityRegistry({}), secrets=StaticSecrets("unused=synthetic"),
                          notifier=notifier)
    flames = flames or (flame(),)
    config = AppConfig(StorageConfig(store.path), flames, tmp_path / "synthetic.toml")
    service = SchedulerService(config=config, storage=store, orchestrator=runner, clock=clock,
                               planner=DailyPlanner(store, randrange=draws or Draws(0)))
    return service, store, clock, notifier, keeper


def test_three_silent_retries_then_one_alert_and_no_more_runs_that_day(tmp_path):
    failed = KeepResult(KeepStatus.FAILED, "offline")
    service, store, clock, notifier, keeper = scheduler(
        tmp_path, [failed] * 4, current=at(11, 9), draws=Draws(3600))
    assert service.run_due() == ([], at(11, 10))
    for instant in (at(11, 10), at(11, 10, 30), at(11, 11, 30), at(11, 13, 30)):
        clock.current = instant
        outcomes, _ = service.run_due()
        assert len(outcomes) == 1
    assert [event["event"] for event in notifier.events] == ["retries_exhausted"]
    state = store.get_state("joinquant-primary", "primary")
    assert state.metadata[RETRIES_EXHAUSTED_KEY] == "2026-08-11"
    assert state.consecutive_failures == 4 and state.next_action_at == at(12, 9)
    alert = notifier.events[0]["result"]
    assert alert.metadata["retry_attempt"] == 4 and alert.metadata["retry_limit"] == 3
    # Nothing else today, and the 20:00 day-without-success alert does not repeat it.
    for instant in (at(11, 15), at(11, 20), at(11, 22, 59)):
        clock.current = instant
        assert service.run_due()[0] == []
    assert [event["event"] for event in notifier.events] == ["retries_exhausted"]
    assert store.count_runs() == 4 and not keeper.results


def test_manual_run_after_exhaustion_does_not_alert_twice(tmp_path):
    failed = KeepResult(KeepStatus.FAILED, "offline")
    service, store, clock, notifier, _ = scheduler(tmp_path, [failed] * 5, current=at(11, 9))
    runner = service.orchestrator
    for minute in (0, 30, 90, 210, 240):
        clock.current = at(11, 9) + timedelta(minutes=minute)
        runner.run_flame(service.config.flames[0], now=clock.current)
    assert [event["event"] for event in notifier.events] == ["retries_exhausted"]


def test_next_day_redraws_and_gets_a_fresh_retry_budget(tmp_path):
    failed = KeepResult(KeepStatus.FAILED, "offline")
    service, store, clock, notifier, _ = scheduler(
        tmp_path, [failed] * 5, current=at(11, 9), draws=Draws(1800))
    runner = service.orchestrator
    for minute in (0, 30, 90, 210):
        clock.current = at(11, 9) + timedelta(minutes=minute)
        runner.run_flame(service.config.flames[0], now=clock.current)
    clock.current = at(12, 9)
    assert service.run_due() == ([], at(12, 9, 30))
    clock.current = at(12, 9, 30)
    outcome = service.run_due()[0][0]
    assert outcome.state.consecutive_failures == 1
    assert outcome.state.next_action_at == at(12, 10)
    assert RETRIES_EXHAUSTED_KEY not in outcome.state.metadata


def test_paused_failure_still_alerts_immediately(tmp_path):
    paused = KeepResult(KeepStatus.FAILED, "stopped", metadata={"automation_paused": True})
    service, store, clock, notifier, _ = scheduler(tmp_path, [paused], current=at(11, 9))
    service.run_due()
    assert [event["event"] for event in notifier.events] == ["flame_failed"]


def test_waiting_human_does_not_spend_or_refund_retries(tmp_path):
    failed = KeepResult(KeepStatus.FAILED, "offline")
    service, store, clock, notifier, _ = scheduler(
        tmp_path, [failed, wait_result(), failed], current=at(11, 9))
    runner = service.orchestrator
    first = runner.run_flame(service.config.flames[0], now=at(11, 9))
    clock.current = at(11, 9, 30)
    waiting = runner.run_flame(service.config.flames[0], now=clock.current)
    assert waiting.state.status is KeepStatus.WAITING_HUMAN
    assert waiting.state.consecutive_failures == 1
    assert waiting.state.next_action_at == at(11, 10, 30)  # recheck_minutes, unchanged
    clock.current = at(11, 10, 30)
    second = runner.run_flame(service.config.flames[0], now=clock.current)
    assert first.record.result.metadata["retry_attempt"] == 1
    assert second.record.result.metadata["retry_attempt"] == 2
    assert second.state.next_action_at == at(11, 11, 30)


def test_short_retry_hint_and_keeper_floor_never_shorten_the_ladder(tmp_path):
    hint = KeepResult(KeepStatus.FAILED, "button", metadata={"same_day_retry_minutes": 30})
    floor = KeepResult(KeepStatus.FAILED, "cooldown", next_action_at=at(11, 14))
    service, store, clock, notifier, _ = scheduler(tmp_path, [hint, hint, floor], current=at(11, 9))
    runner = service.orchestrator
    assert runner.run_flame(flame(), now=at(11, 9)).state.next_action_at == at(11, 9, 30)
    clock.current = at(11, 9, 30)
    assert runner.run_flame(flame(), now=clock.current).state.next_action_at == at(11, 10, 30)
    clock.current = at(11, 10, 30)
    assert runner.run_flame(flame(), now=clock.current).state.next_action_at == at(11, 14)


def test_staggered_pair_in_the_scheduler(tmp_path):
    config, checkin, reading = pair()
    done = KeepResult(KeepStatus.SUCCESS, "done")
    # check-in draws 09:20, reading draws 09:00 -> reading due 09:25 and 5 minutes after check-in ran.
    service, store, clock, notifier, keeper = scheduler(
        tmp_path, [done, done], current=at(11, 9), draws=Draws(1200, 0), flames=(checkin, reading))
    assert service.run_due() == ([], at(11, 9, 20))
    clock.current = at(11, 9, 20)
    outcomes, next_due = service.run_due()
    assert [item.record.flame_id for item in outcomes] == ["joinquant-primary"]
    assert next_due == at(11, 9, 25)
    clock.current = at(11, 9, 25)
    assert [item.record.flame_id for item in service.run_due()[0]] == ["joinquant-reading-primary"]


# --- browser stop floors -----------------------------------------------------------

def test_browser_stop_has_no_fixed_delay_for_non_human_failures():
    context = SimpleNamespace(clock=FixedClock(at(11, 10)), now=at(11, 10))
    stopped = JoinQuantBrowserKeeper._stop(context, "status_unavailable")
    assert stopped.status is KeepStatus.FAILED and stopped.next_action_at is None
    human = JoinQuantBrowserKeeper._stop(context, "login_required", human=True)
    assert human.next_action_at == at(11, 10) + timedelta(hours=24)
    cooled = JoinQuantBrowserKeeper._stop(context, "status_http", status=429, retry_after="7200")
    assert cooled.next_action_at == at(11, 12)
