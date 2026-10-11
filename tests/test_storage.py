from datetime import datetime, timezone
from pathlib import Path

import pytest

from keepflame.core.models import FlameState, KeepResult, KeepStatus, RunRecord
from keepflame.storage import SQLiteStorage
from keepflame.storage.sqlite import StorageError


def record_at(run_id: str, message: str, checked_at: datetime) -> tuple[RunRecord, FlameState]:
    result = KeepResult(
        status=KeepStatus.SUCCESS,
        message=message,
        metadata={"task_status": 1},
        next_action_at=checked_at,
    )
    run = RunRecord(
        run_id=run_id,
        flame_id="joinquant-primary",
        account_id="primary",
        started_at=checked_at,
        finished_at=checked_at,
        result=result,
    )
    state = FlameState(
        flame_id=run.flame_id,
        account_id=run.account_id,
        status=result.status,
        message=message,
        last_checked_at=checked_at,
        last_success_at=checked_at,
        consecutive_failures=0,
        pending_human_since=None,
        human_action=None,
        next_action_at=checked_at,
        last_reminder_at=None,
        metadata=dict(result.metadata),
    )
    return run, state


def test_state_and_run_history_are_written_together(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "state.sqlite3")
    now = datetime(2026, 8, 11, 1, tzinfo=timezone.utc)
    run, state = record_at("run-1", "healthy", now)
    storage.record(run, state)
    loaded = storage.get_state("joinquant-primary", "primary")
    assert loaded == state
    assert storage.count_runs() == 1
    storage.healthcheck()


def test_duplicate_run_rolls_back_state_update(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "state.sqlite3")
    now = datetime(2026, 8, 11, 1, tzinfo=timezone.utc)
    original_run, original_state = record_at("same-run", "original", now)
    storage.record(original_run, original_state)
    duplicate_run, duplicate_state = record_at("same-run", "must-not-win", now)
    with pytest.raises(StorageError, match="failed to record"):
        storage.record(duplicate_run, duplicate_state)
    assert storage.get_state("joinquant-primary", "primary").message == "original"
    assert storage.count_runs() == 1


def test_multiple_accounts_remain_separate(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "state.sqlite3")
    now = datetime(2026, 8, 11, 1, tzinfo=timezone.utc)
    run, state = record_at("a", "account-a", now)
    storage.record(run, state)
    run_b, state_b = record_at("b", "account-b", now)
    run_b = RunRecord(
        run_id=run_b.run_id,
        flame_id="joinquant-secondary",
        account_id="secondary",
        started_at=run_b.started_at,
        finished_at=run_b.finished_at,
        result=run_b.result,
    )
    state_b = FlameState(
        flame_id=run_b.flame_id,
        account_id=run_b.account_id,
        status=state_b.status,
        message=state_b.message,
        last_checked_at=state_b.last_checked_at,
        last_success_at=state_b.last_success_at,
        consecutive_failures=0,
        pending_human_since=None,
        human_action=None,
        next_action_at=state_b.next_action_at,
        last_reminder_at=None,
        metadata=state_b.metadata,
    )
    storage.record(run_b, state_b)
    assert len(storage.list_states()) == 2



def test_corrupt_metadata_json_raises_storage_error(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "state.sqlite3")
    now = datetime(2026, 8, 11, 1, tzinfo=timezone.utc)
    run, state = record_at("run-1", "healthy", now)
    storage.record(run, state)
    import sqlite3

    with sqlite3.connect(storage.path) as db:
        db.execute(
            "UPDATE flame_state SET metadata_json = ? WHERE flame_id = ? AND account_id = ?",
            ("NOT-JSON{", "joinquant-primary", "primary"),
        )
    with pytest.raises(StorageError, match="corrupt"):
        storage.get_state("joinquant-primary", "primary")
    with pytest.raises(StorageError, match="corrupt"):
        storage.list_states()


def test_corrupt_pending_notification_json_raises_storage_error(tmp_path: Path) -> None:
    import sqlite3

    from keepflame.core.models import PendingNotification

    storage = SQLiteStorage(tmp_path / "state.sqlite3")
    now = datetime(2026, 8, 11, 1, tzinfo=timezone.utc)
    run, state = record_at("run-1", "healthy", now)
    notification = PendingNotification(
        notification_id="n1",
        flame_id=run.flame_id,
        account_id=run.account_id,
        event="flame_failed",
        result=run.result,
        occurred_at=now,
        retry_at=now,
    )
    storage.record(run, state, notification)
    with sqlite3.connect(storage.path) as db:
        db.execute(
            "UPDATE pending_notifications SET payload_json = ? WHERE flame_id = ? AND account_id = ?",
            ("{bad", run.flame_id, run.account_id),
        )
    with pytest.raises(StorageError, match="corrupt"):
        storage.get_notification(run.flame_id, run.account_id)
