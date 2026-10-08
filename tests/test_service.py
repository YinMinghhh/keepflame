from datetime import time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.config import AppConfig, FlameConfig, ScheduleConfig, StorageConfig
from keepflame.core.models import HumanAction, KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.service import SchedulerService
from tests.helpers import (
    FakeNotifier,
    FixedClock,
    MemoryStorage,
    SequenceKeeper,
    StaticRegistry,
    StaticSecrets,
)

ZONE = ZoneInfo("Asia/Shanghai")


def local(day: int, hour: int):
    from datetime import datetime

    return datetime(2026, 8, day, hour, tzinfo=ZONE).astimezone(timezone.utc)


def test_scheduler_resumes_persisted_next_action_after_restart(tmp_path: Path) -> None:
    flame = FlameConfig(
        id="joinquant-primary",
        keeper="fake",
        account_id="primary",
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
    config = AppConfig(
        storage=StorageConfig(tmp_path / "unused.sqlite3"),
        flames=(flame,),
        source_path=tmp_path / "keepflame.toml",
    )
    storage = MemoryStorage()
    shared = {
        "storage": storage,
        "capabilities": CapabilityRegistry({}),
        "secrets": StaticSecrets("unused=value"),
        "notifier": FakeNotifier(),
    }

    clock = FixedClock(local(11, 9))
    shared["clock"] = clock
    first_runner = Orchestrator(
        registry=StaticRegistry(
            SequenceKeeper(
                KeepResult(
                    status=KeepStatus.WAITING_HUMAN,
                    message="waiting",
                    human_action=HumanAction(
                        kind="SIGN_IN",
                        summary="Sign in",
                        instructions="Use the official page",
                    ),
                )
            )
        ),
        **shared,
    )
    first_runner.run_flame(flame, now=local(11, 9), notify=False)
    assert storage.get_state(flame.id, flame.account_id).next_action_at == local(11, 10)

    restarted_runner = Orchestrator(
        registry=StaticRegistry(
            SequenceKeeper(KeepResult(status=KeepStatus.SUCCESS, message="healthy"))
        ),
        **shared,
    )
    clock.current = local(11, 10)
    service = SchedulerService(
        config=config,
        storage=storage,
        orchestrator=restarted_runner,
        clock=clock,
    )
    outcomes, next_due = service.run_due()
    assert len(outcomes) == 1
    assert outcomes[0].record.result.status is KeepStatus.SUCCESS
    assert next_due == local(12, 9)

