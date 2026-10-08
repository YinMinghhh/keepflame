import io
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.config import FlameConfig, ScheduleConfig
from keepflame.core.models import HumanAction, KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.flames.joinquant import JoinQuantKeeper
from keepflame.notifier import JsonlNotifier
from keepflame.storage import SQLiteStorage
from tests.helpers import (
    FixedClock,
    FakeHttpClient,
    FakeNotifier,
    MemoryStorage,
    SequenceKeeper,
    StaticRegistry,
    StaticSecrets,
    response,
)

ZONE = ZoneInfo("Asia/Shanghai")


def local(day: int, hour: int) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=ZONE).astimezone(timezone.utc)


def flame(account_id: str = "primary", flame_id: str = "joinquant-primary") -> FlameConfig:
    return FlameConfig(
        id=flame_id,
        keeper="fake",
        account_id=account_id,
        enabled=True,
        schedule=ScheduleConfig(
            timezone="Asia/Shanghai",
            first_check=time(9),
            recheck_minutes=60,
            reminder_at=time(20),
            stop_at=time(23),
        ),
        settings={},
    )


def wait_result(kind: str = "SIGN_IN") -> KeepResult:
    return KeepResult(
        status=KeepStatus.WAITING_HUMAN,
        message="waiting",
        human_action=HumanAction(
            kind=kind,
            summary="Needs help",
            instructions="Use the official page",
        ),
    )


def orchestrator_for(results, storage=None, notifier=None) -> Orchestrator:
    return Orchestrator(
        storage=storage or MemoryStorage(),
        registry=StaticRegistry(SequenceKeeper(*results)),
        capabilities=CapabilityRegistry({}),
        secrets=StaticSecrets("unused=value"),
        notifier=notifier or FakeNotifier(),
        clock=FixedClock(local(11, 9)),
    )


def test_waiting_reminder_and_recovery_are_low_noise() -> None:
    storage = MemoryStorage()
    notifier = FakeNotifier()
    runner = orchestrator_for(
        [
            wait_result(),
            wait_result(),
            wait_result(),
            KeepResult(status=KeepStatus.SUCCESS, message="healthy"),
        ],
        storage,
        notifier,
    )

    runner.clock.current = local(11, 9)
    first = runner.run_flame(flame(), now=local(11, 9))
    runner.clock.current = local(11, 10)
    repeated = runner.run_flame(flame(), now=local(11, 10))
    runner.clock.current = local(11, 20)
    reminder = runner.run_flame(flame(), now=local(11, 20))
    runner.clock.current = local(11, 21)
    recovered = runner.run_flame(flame(), now=local(11, 21))

    assert first.notification_event == "human_action_required"
    assert repeated.notification_event is None
    assert reminder.notification_event == "human_action_reminder"
    assert recovered.notification_event == "flame_recovered"
    assert [event["event"] for event in notifier.events] == [
        "human_action_required",
        "human_action_reminder",
        "flame_recovered",
    ]
    assert len(storage.runs) == 4
    assert recovered.state.pending_human_since is None
    assert recovered.state.next_action_at == local(12, 9)


def test_waiting_notifies_again_on_next_schedule_day() -> None:
    storage = MemoryStorage()
    runner = orchestrator_for([wait_result(), wait_result()], storage)
    runner.clock.current = local(11, 9)
    runner.run_flame(flame(), now=local(11, 9), notify=False)
    runner.clock.current = local(12, 9)
    next_day = runner.run_flame(flame(), now=local(12, 9), notify=False)
    assert next_day.notification_event == "human_action_required"


def test_failures_increment_and_identical_failure_is_silent() -> None:
    storage = MemoryStorage()
    runner = orchestrator_for(
        [
            KeepResult(status=KeepStatus.FAILED, message="network failed"),
            KeepResult(status=KeepStatus.FAILED, message="network failed"),
            KeepResult(status=KeepStatus.SUCCESS, message="healthy"),
        ],
        storage,
    )
    runner.clock.current = local(11, 9)
    first = runner.run_flame(flame(), now=local(11, 9), notify=False)
    runner.clock.current = local(11, 10)
    second = runner.run_flame(flame(), now=local(11, 10), notify=False)
    runner.clock.current = local(11, 11)
    recovered = runner.run_flame(flame(), now=local(11, 11), notify=False)
    assert first.state.consecutive_failures == 1
    assert second.state.consecutive_failures == 2
    assert second.notification_event is None
    assert recovered.state.consecutive_failures == 0
    assert recovered.notification_event == "flame_recovered"


def test_account_state_is_isolated() -> None:
    storage = MemoryStorage()
    runner = orchestrator_for(
        [
            KeepResult(status=KeepStatus.SUCCESS, message="one"),
            KeepResult(status=KeepStatus.SUCCESS, message="two"),
        ],
        storage,
    )
    runner.clock.current = local(11, 9)
    runner.run_flame(flame("a", "joinquant-a"), now=local(11, 9), notify=False)
    runner.clock.current = local(11, 9)
    runner.run_flame(flame("b", "joinquant-b"), now=local(11, 9), notify=False)
    assert storage.get_state("joinquant-a", "a").message == "one"
    assert storage.get_state("joinquant-b", "b").message == "two"


def test_cookie_never_reaches_sqlite_or_jsonl(tmp_path: Path) -> None:
    cookie = "sid=highly-sensitive-value; account=private"
    http = FakeHttpClient(
        response(200, '{"code":"00000","data":{"taskList":{"login":{"status":0}}}}')
    )
    storage = SQLiteStorage(tmp_path / "keepflame.sqlite3")
    stream = io.StringIO()
    runner = Orchestrator(
        storage=storage,
        registry=StaticRegistry(JoinQuantKeeper(sleeper=lambda _seconds: None)),
        capabilities=CapabilityRegistry({"http": http}),
        secrets=StaticSecrets(cookie),
        notifier=JsonlNotifier(stream),
        clock=FixedClock(local(11, 9)),
    )
    config = flame()
    config = FlameConfig(
        id=config.id,
        keeper=config.keeper,
        account_id=config.account_id,
        enabled=True,
        schedule=config.schedule,
        settings={"cookie_file": "/run/secrets/joinquant.cookie"},
    )
    runner.clock.current = local(11, 9)
    runner.run_flame(config, now=local(11, 9))
    assert cookie not in stream.getvalue()
    assert cookie.encode() not in (tmp_path / "keepflame.sqlite3").read_bytes()
