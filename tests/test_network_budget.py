"""Network-only failures spend a separate, bounded per-day budget, not retry_minutes."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.capabilities.secrets import FileSecretResolver
from keepflame.core.config import ConfigError, FlameConfig, ScheduleConfig, load_config
from keepflame.core.models import FAILURE_CLASS_TRANSPORT, KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.schedule import RETRIES_EXHAUSTED_KEY, due_at
from tests.helpers import FakeNotifier, FixedClock, MemoryStorage, StaticRegistry

ZONE = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 10, 20, 9, 30, tzinfo=ZONE).astimezone(timezone.utc)


def network(**extra):
    return KeepResult(KeepStatus.FAILED, "tasks_transport_error",
                      metadata={"reason": "tasks_transport_error", "failure_class": FAILURE_CLASS_TRANSPORT,
                                "error_type": "SSLEOFError", **extra})


def business():
    return KeepResult(KeepStatus.FAILED, "sign_rejected", metadata={"reason": "sign_rejected"})


class Harness:
    def __init__(self, **schedule):
        self.storage, self.notifier, self.clock = MemoryStorage(), FakeNotifier(), FixedClock(START)
        self.results = []
        keeper = type("K", (), {"business_timezone": "Asia/Shanghai",
                                "keep": lambda _self, _ctx: self.results.pop(0)})()
        self.runner = Orchestrator(storage=self.storage, registry=StaticRegistry(keeper),
                                   capabilities=CapabilityRegistry({}), secrets=FileSecretResolver(),
                                   notifier=self.notifier, clock=self.clock)
        self.flame = FlameConfig("demo", "demo", "primary", True, ScheduleConfig(
            "Asia/Shanghai", time(9), 60, time(20), time(23), **schedule), {})

    def run(self, result, at=None):
        if at is not None:
            self.clock.current = at
        self.results.append(result)
        outcome = self.runner.run_flame(self.flame, now=self.clock.current)
        return outcome

    def run_when_due(self, result):
        due = due_at(self.flame.schedule, self.storage.get_state("demo", "primary"), self.clock.current)
        return self.run(result, at=due)

    @property
    def events(self):
        return [event["event"] for event in self.notifier.events]


def test_network_failures_keep_the_business_retry_counter_untouched():
    h = Harness()
    for attempt in range(1, 4):
        before = h.clock.current
        outcome = h.run_when_due(network())
        meta = outcome.record.result.metadata
        assert outcome.state.consecutive_failures == 0 and meta["retry_attempt"] == 0
        assert meta["transport_failure_attempt"] == attempt and meta["transport_failure_cap"] == 6
        assert meta["error_type"] == "SSLEOFError" and "retries_exhausted" not in meta
        assert outcome.state.next_action_at == h.clock.current + timedelta(minutes=10)
        assert h.clock.current >= before
    assert h.events == []


def test_cap_plus_one_network_failure_sends_one_retries_exhausted_alert_naming_the_network():
    h = Harness(transport_failure_cap=3, transport_retry_minutes=5)
    for _ in range(3):
        assert "retries_exhausted" not in h.run_when_due(network()).record.result.metadata
    outcome = h.run_when_due(network())
    meta = outcome.record.result.metadata
    assert meta["retries_exhausted"] is True and meta["exhausted_cause"] == "network"
    assert "cause: network" in outcome.record.result.message
    assert outcome.state.metadata[RETRIES_EXHAUSTED_KEY] == "2026-10-20"
    assert outcome.state.consecutive_failures == 0
    assert h.events == ["retries_exhausted"]
    assert outcome.state.next_action_at == datetime(2026, 10, 21, 9, tzinfo=ZONE).astimezone(timezone.utc)
    # Nothing runs again today.
    assert due_at(h.flame.schedule, outcome.state, h.clock.current) == outcome.state.next_action_at


def test_business_and_network_budgets_are_counted_apart_and_both_survive_each_other():
    h = Harness()
    first = h.run_when_due(business())
    assert first.state.consecutive_failures == 1 and first.record.result.metadata["retry_attempt"] == 1
    for count in (1, 2):
        outcome = h.run_when_due(network())
        assert outcome.state.consecutive_failures == 1
        assert outcome.record.result.metadata["transport_failure_attempt"] == count
    again = h.run_when_due(business())
    assert again.state.consecutive_failures == 2 and again.record.result.metadata["retry_attempt"] == 2
    assert again.state.metadata["transport_failures"] == {"day": "2026-10-20", "count": 2}
    assert h.run_when_due(network()).record.result.metadata["transport_failure_attempt"] == 3


def test_stop_at_bounds_the_network_budget():
    h = Harness()
    late = datetime(2026, 10, 20, 22, 55, tzinfo=ZONE).astimezone(timezone.utc)
    outcome = h.run(network(), at=late)
    meta = outcome.record.result.metadata
    assert meta["transport_failure_attempt"] == 1 and meta["exhausted_cause"] == "network"
    assert h.events == ["retries_exhausted"]


def test_a_cooldown_floor_from_the_keeper_is_still_respected():
    h = Harness()
    floor = START + timedelta(hours=2)
    result = network()
    outcome = h.run(KeepResult(result.status, result.message, metadata=result.metadata, next_action_at=floor))
    assert outcome.state.next_action_at == floor


def test_next_business_day_starts_a_fresh_network_budget():
    h = Harness(transport_failure_cap=1)
    h.run(network())
    assert h.run_when_due(network()).record.result.metadata["exhausted_cause"] == "network"
    tomorrow = datetime(2026, 10, 21, 9, 5, tzinfo=ZONE).astimezone(timezone.utc)
    outcome = h.run(network(), at=tomorrow)
    assert outcome.record.result.metadata["transport_failure_attempt"] == 1
    assert "retries_exhausted" not in outcome.record.result.metadata


def test_cap_zero_sends_network_failures_to_the_business_ladder():
    h = Harness(transport_failure_cap=0)
    outcome = h.run(network())
    assert outcome.state.consecutive_failures == 1 and "transport_failure_attempt" not in outcome.record.result.metadata
    assert outcome.state.next_action_at == START + timedelta(minutes=30)


def test_paused_or_unmarked_failures_never_use_the_network_budget():
    h = Harness()
    paused = h.run(network(automation_paused=True))
    assert paused.state.consecutive_failures == 1 and "transport_failure_attempt" not in paused.record.result.metadata
    assert h.events == ["flame_failed"]


@pytest.mark.parametrize("field, value", [
    ("transport_failure_cap", -1), ("transport_failure_cap", 25), ("transport_failure_cap", True),
    ("transport_retry_minutes", 0), ("transport_retry_minutes", 121), ("transport_retry_minutes", 1.5),
])
def test_network_budget_settings_are_validated(field, value):
    with pytest.raises(ConfigError):
        ScheduleConfig("Asia/Shanghai", time(9), 60, time(20), time(23), **{field: value})


def test_network_budget_is_read_from_the_schedule_table(tmp_path):
    path = tmp_path / "keepflame.toml"
    path.write_text(f"""
[storage]
path = "{tmp_path / 'state.sqlite3'}"

[[flames]]
id = "demo"
keeper = "demo"
account_id = "primary"

[flames.schedule]
timezone = "Asia/Shanghai"
first_check = "09:00"
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"
transport_failure_cap = 4
transport_retry_minutes = 3
""")
    schedule = load_config(path).flames[0].schedule
    assert (schedule.transport_failure_cap, schedule.transport_retry_minutes) == (4, 3)
