import io
import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.config import FlameConfig, ScheduleConfig
from keepflame.core.models import HumanAction, KeepResult, KeepStatus, Reward, RewardSource
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


def _credit(item, at, *, proven=True, amount=1):
    line = {"proven": proven, "amount": amount, "item": item, "at": at}
    reward = Reward("1", "积分", RewardSource.OBSERVED) if proven else None
    return KeepResult(KeepStatus.SUCCESS, item, reward=reward, metadata={"credit_line": line})


def test_proven_credit_line_notifies_even_after_previous_success():
    today = datetime(2026, 10, 11, 10, 9, tzinfo=ZONE)
    yesterday = datetime(2026, 10, 10, 14, 41, tzinfo=ZONE)
    stream = io.StringIO()
    storage = MemoryStorage()
    checkin = flame(flame_id="joinquant-primary")
    reading = flame(flame_id="joinquant-reading-primary")
    unproven = flame(flame_id="joinquant-unproven")
    replay = flame(flame_id="joinquant-replay")
    runner = Orchestrator(
        storage=storage,
        registry=StaticRegistry(SequenceKeeper(
            KeepResult(KeepStatus.SUCCESS, "already healthy"),
            _credit("签到成功", "2026-10-11T10:09:15+08:00"),
            KeepResult(KeepStatus.SUCCESS, "already healthy"),
            _credit("浏览文章", "2026-10-11T10:04:45+08:00"),
            _credit("浏览文章", "2026-10-10T14:41:00+08:00"),
            KeepResult(KeepStatus.FAILED, "not proven yet"),
            _credit("签到成功", "2026-10-11T10:09:15+08:00", proven=False, amount=None),
            _credit("签到成功", "2026-10-10T22:23:31+08:00"),
        )),
        capabilities=CapabilityRegistry({}),
        secrets=StaticSecrets("unused=value"),
        notifier=JsonlNotifier(stream),
        clock=FixedClock(today),
    )

    runner.clock.current = today
    prior = runner.run_flame(checkin, now=today, notify=False)
    assert prior.state.status is KeepStatus.SUCCESS and prior.notification_event is None
    recorded = runner.run_flame(checkin, now=today)
    assert recorded.notification_event == "credit_recorded"

    runner.run_flame(reading, now=today, notify=False)
    reading_note = runner.run_flame(reading, now=today)
    assert reading_note.notification_event == "credit_recorded"

    older = runner.run_flame(reading, now=today)
    assert older.notification_event is None
    assert older.record.result.metadata["credit_line"]["at"] == "2026-10-10T14:41:00+08:00"
    assert older.record.result.metadata["business_target"]["day"] == "2026-10-11"

    runner.run_flame(unproven, now=today, notify=False)
    missing = runner.run_flame(unproven, now=today)
    assert missing.notification_event == "flame_recovered"
    assert missing.record.result.metadata["credit_line"]["amount"] is None
    assert missing.record.result.reward is None

    runner.clock.current = yesterday
    held = runner.run_flame(replay, now=yesterday, notify=False)
    assert held.notification_event == "credit_recorded"
    pending = storage.get_notification(replay.id, replay.account_id)
    assert pending.business_target.day == "2026-10-10"
    runner.clock.current = today
    runner.deliver_pending(replay, now=today)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    by_flame = {}
    for line in lines:
        by_flame.setdefault(line["flame_id"], []).append(line)
    checkin_line = by_flame["joinquant-primary"][0]
    reading_line = by_flame["joinquant-reading-primary"][0]
    assert checkin_line["event"] == "credit_recorded"
    assert checkin_line["metadata"]["credit_line"] == {
        "proven": True, "amount": 1, "item": "签到成功", "at": "2026-10-11T10:09:15+08:00",
    }
    assert checkin_line["reward"]["amount"] == "1"
    assert reading_line["event"] == "credit_recorded"
    assert reading_line["metadata"]["credit_line"]["item"] == "浏览文章"
    assert reading_line["metadata"]["credit_line"]["at"] == "2026-10-11T10:04:45+08:00"
    assert reading_line["metadata"]["credit_line"]["amount"] == 1
    assert all(line["metadata"]["business_target"]["day"] == "2026-10-11" for line in by_flame["joinquant-reading-primary"])
    recovered = by_flame["joinquant-unproven"][0]
    assert recovered["event"] == "flame_recovered"
    assert recovered["metadata"]["credit_line"]["amount"] is None
    assert recovered["reward"] is None
    replayed = by_flame["joinquant-replay"][0]
    assert replayed["event"] == "credit_recorded"
    assert replayed["metadata"]["business_target"]["day"] == "2026-10-10"
    assert replayed["metadata"]["credit_line"]["at"] == "2026-10-10T22:23:31+08:00"
    assert replayed["completion_receipt"]["target"]["day"] == "2026-10-10"
    print("observation credit_recorded_after_previous_success checkin=签到成功 reading=浏览文章")
    print("observation older_credit_line_not_relabeled_as_today=true")
    print("observation replay_keeps_original_day=2026-10-10")
    print("observation unproven_amount=null reward=null")
