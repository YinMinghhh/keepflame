"""SQLite persistence for current Flame state and immutable run history."""

from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
from dataclasses import asdict
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from keepflame.core.models import (
    FlameState,
    HumanAction,
    KeepStatus,
    KeepResult,
    PendingNotification,
    Reward,
    RewardSource,
    RunRecord,
)

from keepflame.storage.control import ControlStorageMixin

SCHEMA_VERSION = 3


class StorageError(RuntimeError):
    """Raised when the database cannot preserve lifecycle state safely."""


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageError("database contains a timezone-naive timestamp")
    return parsed.astimezone(timezone.utc)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _action_json(action: HumanAction | None) -> str | None:
    return None if action is None else _json(action.to_dict())


def _action(value: str | None) -> HumanAction | None:
    if value is None:
        return None
    raw = json.loads(value)
    return HumanAction(
        kind=raw["kind"],
        summary=raw["summary"],
        instructions=raw["instructions"],
        url=raw.get("url"),
    )


def _notification_json(value: PendingNotification) -> str:
    raw = asdict(value)
    for name in ("occurred_at", "retry_at", "reminder_at"):
        raw[name] = _timestamp(getattr(value, name))
    raw["result"]["next_action_at"] = _timestamp(value.result.next_action_at)
    return _json(raw)


def _notification(value: str) -> PendingNotification:
    raw = json.loads(value)
    result = raw.pop("result")
    result["status"] = KeepStatus(result["status"])
    result["next_action_at"] = _datetime(result["next_action_at"])
    if result["human_action"] is not None:
        result["human_action"] = HumanAction(**result["human_action"])
    if result["reward"] is not None:
        result["reward"]["source"] = RewardSource(result["reward"]["source"])
        result["reward"] = Reward(**result["reward"])
    for name in ("occurred_at", "retry_at", "reminder_at"):
        raw[name] = _datetime(raw[name])
    return PendingNotification(result=KeepResult(**result), **raw)


class SQLiteStorage(ControlStorageMixin):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        version INTEGER NOT NULL
                    )
                    """
                )
                version_row = connection.execute(
                    "SELECT version FROM schema_meta LIMIT 1"
                ).fetchone()
                if version_row is None:
                    connection.execute(
                        "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
                    )
                elif version_row["version"] not in (1, 2, SCHEMA_VERSION):
                    raise StorageError(
                        f"unsupported database schema version: {version_row['version']}"
                    )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS flame_state (
                        flame_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT NOT NULL,
                        last_checked_at TEXT NOT NULL,
                        last_success_at TEXT,
                        consecutive_failures INTEGER NOT NULL CHECK(consecutive_failures >= 0),
                        pending_human_since TEXT,
                        human_action_json TEXT,
                        next_action_at TEXT,
                        last_reminder_at TEXT,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY (flame_id, account_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS keep_runs (
                        run_id TEXT PRIMARY KEY,
                        flame_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT NOT NULL,
                        reward_json TEXT,
                        metadata_json TEXT NOT NULL,
                        next_action_at TEXT,
                        human_action_json TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS keep_runs_flame_time
                    ON keep_runs(flame_id, account_id, started_at)
                    """
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS pending_notifications (
                        flame_id TEXT NOT NULL, account_id TEXT NOT NULL,
                        notification_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
                        PRIMARY KEY (flame_id, account_id)
                    )"""
                )
                self._initialize_control(connection)
                connection.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
        except sqlite3.Error as exc:
            raise StorageError(f"failed to initialize SQLite database: {self.path}") from exc

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> FlameState:
        return FlameState(
            flame_id=row["flame_id"],
            account_id=row["account_id"],
            status=KeepStatus(row["status"]),
            message=row["message"],
            last_checked_at=_datetime(row["last_checked_at"]),  # type: ignore[arg-type]
            last_success_at=_datetime(row["last_success_at"]),
            consecutive_failures=row["consecutive_failures"],
            pending_human_since=_datetime(row["pending_human_since"]),
            human_action=_action(row["human_action_json"]),
            next_action_at=_datetime(row["next_action_at"]),
            last_reminder_at=_datetime(row["last_reminder_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def get_state(self, flame_id: str, account_id: str) -> FlameState | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM flame_state
                    WHERE flame_id = ? AND account_id = ?
                    """,
                    (flame_id, account_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("failed to read Flame state") from exc
        return None if row is None else self._state_from_row(row)

    def list_states(self) -> list[FlameState]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM flame_state ORDER BY flame_id, account_id"
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("failed to list Flame states") from exc
        return [self._state_from_row(row) for row in rows]

    def record(
        self, run: RunRecord, state: FlameState,
        notification: PendingNotification | None = None,
    ) -> None:
        if (run.flame_id, run.account_id) != (state.flame_id, state.account_id):
            raise StorageError("run and state belong to different Flame accounts")
        if notification is not None and (notification.flame_id, notification.account_id) != (run.flame_id, run.account_id):
            raise StorageError("notification and run belong to different Flame accounts")
        reward_json = None if run.result.reward is None else _json(run.result.reward.to_dict())
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO keep_runs(
                        run_id, flame_id, account_id, started_at, finished_at,
                        status, message, reward_json, metadata_json,
                        next_action_at, human_action_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        run.flame_id,
                        run.account_id,
                        _timestamp(run.started_at),
                        _timestamp(run.finished_at),
                        run.result.status.value,
                        run.result.message,
                        reward_json,
                        _json(run.result.metadata),
                        _timestamp(run.result.next_action_at),
                        _action_json(run.result.human_action),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO flame_state(
                        flame_id, account_id, status, message, last_checked_at,
                        last_success_at, consecutive_failures, pending_human_since,
                        human_action_json, next_action_at, last_reminder_at,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(flame_id, account_id) DO UPDATE SET
                        status = excluded.status,
                        message = excluded.message,
                        last_checked_at = excluded.last_checked_at,
                        last_success_at = excluded.last_success_at,
                        consecutive_failures = excluded.consecutive_failures,
                        pending_human_since = excluded.pending_human_since,
                        human_action_json = excluded.human_action_json,
                        next_action_at = excluded.next_action_at,
                        last_reminder_at = excluded.last_reminder_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        state.flame_id,
                        state.account_id,
                        state.status.value,
                        state.message,
                        _timestamp(state.last_checked_at),
                        _timestamp(state.last_success_at),
                        state.consecutive_failures,
                        _timestamp(state.pending_human_since),
                        _action_json(state.human_action),
                        _timestamp(state.next_action_at),
                        _timestamp(state.last_reminder_at),
                        _json(state.metadata),
                    ),
                )
                connection.execute(
                    "DELETE FROM pending_notifications WHERE flame_id = ? AND account_id = ?",
                    (run.flame_id, run.account_id),
                )
                if notification is not None:
                    connection.execute(
                        "INSERT INTO pending_notifications VALUES (?, ?, ?, ?)",
                        (run.flame_id, run.account_id, notification.notification_id, _notification_json(notification)),
                    )
        except sqlite3.Error as exc:
            raise StorageError("failed to record Keeper run") from exc

    def count_runs(self, flame_id: str | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM keep_runs"
        parameters: tuple[str, ...] = ()
        if flame_id is not None:
            query += " WHERE flame_id = ?"
            parameters = (flame_id,)
        try:
            with self._connect() as connection:
                row = connection.execute(query, parameters).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("failed to count Keeper runs") from exc
        return int(row["count"])

    def healthcheck(self) -> None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT version FROM schema_meta LIMIT 1"
                ).fetchone()
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            raise StorageError("SQLite healthcheck failed") from exc
        if row is None or row["version"] != SCHEMA_VERSION:
            raise StorageError("SQLite schema is not ready")



    def get_notification(self, flame_id: str, account_id: str) -> PendingNotification | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM pending_notifications WHERE flame_id = ? AND account_id = ?",
                    (flame_id, account_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("failed to read pending notification") from exc
        return None if row is None else _notification(row["payload_json"])

    def update_notification(self, notification: PendingNotification) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE pending_notifications SET payload_json = ? WHERE notification_id = ?",
                    (_notification_json(notification), notification.notification_id),
                )
        except sqlite3.Error as exc:
            raise StorageError("failed to defer notification") from exc

    def acknowledge_notification(self, notification: PendingNotification) -> None:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                deleted = connection.execute(
                    "DELETE FROM pending_notifications WHERE notification_id = ?",
                    (notification.notification_id,),
                ).rowcount
                if deleted and notification.reminder_at is not None:
                    connection.execute(
                        "UPDATE flame_state SET last_reminder_at = ? WHERE flame_id = ? AND account_id = ?",
                        (_timestamp(notification.reminder_at), notification.flame_id, notification.account_id),
                    )
        except sqlite3.Error as exc:
            raise StorageError("failed to acknowledge notification") from exc

    @contextmanager
    def execution_lock(self):
        """One runner per database; status and health remain available."""
        path = Path(str(self.path.resolve()) + ".runner.lock")
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StorageError("another KeepFlame runner is already using this database") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
