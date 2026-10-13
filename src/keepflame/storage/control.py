"""Durable write intents, operator pauses and local login requests.

Only identifiers and normalized states are stored here, never browser material.
"""

import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from keepflame.core.eligibility import zero_claim_pending_allows_new_day, zero_claim_pending_allows_retry
from keepflame.core.models import ActionAttempt


def stamp(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("control timestamps must be timezone-aware")
    return now.astimezone(timezone.utc).isoformat()


class ControlStorageMixin:
    @staticmethod
    def _initialize_control(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS action_attempts (
            keeper TEXT NOT NULL, account_id TEXT NOT NULL, task TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','confirmed')),
            day TEXT NOT NULL, started_at TEXT NOT NULL, confirmed_at TEXT,
            PRIMARY KEY(keeper, account_id, task))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS runtime_pauses (
            keeper TEXT NOT NULL, account_id TEXT NOT NULL, scope TEXT NOT NULL,
            reason TEXT NOT NULL, paused_at TEXT NOT NULL,
            PRIMARY KEY(keeper, account_id, scope))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS login_requests (
            id TEXT PRIMARY KEY, keeper TEXT NOT NULL, account_id TEXT NOT NULL,
            flame_id TEXT NOT NULL, state TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
        connection.execute("DROP INDEX IF EXISTS one_active_login")
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_login
            ON login_requests(keeper, account_id)
            WHERE state IN ('requested','opening','waiting','confirmed','cancelled')""")

        connection.execute("""CREATE TABLE IF NOT EXISTS runtime_commands (
            id TEXT PRIMARY KEY, command TEXT NOT NULL CHECK(command='reload'),
            state TEXT NOT NULL, created_at TEXT NOT NULL, error_type TEXT)""")
        # Additive table: older code ignores it, so no schema version bump.
        connection.execute("""CREATE TABLE IF NOT EXISTS reading_articles (
            keeper TEXT NOT NULL, account_id TEXT NOT NULL, article_id TEXT NOT NULL,
            day TEXT NOT NULL, read_at TEXT NOT NULL,
            PRIMARY KEY(keeper, account_id, article_id))""")

    def read_article_ids(self, keeper, account_id):
        with self._connect() as connection:
            rows = connection.execute("SELECT article_id FROM reading_articles WHERE keeper=? AND account_id=?",
                                      (keeper, account_id)).fetchall()
        return {row["article_id"] for row in rows}

    def record_article_read(self, keeper, account_id, article_id, day, now):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("article day must be an ISO calendar date")
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO reading_articles VALUES (?,?,?,?,?)",
                               (keeper, account_id, str(article_id), day, stamp(now)))

    def request_reload(self, now):
        identifier = str(uuid4())
        with self._connect() as connection:
            connection.execute("INSERT INTO runtime_commands VALUES (?,'reload','requested',?,NULL)", (identifier, stamp(now)))
        return identifier

    def runtime_commands(self, *, include_finished=False):
        with self._connect() as connection:
            query = "SELECT * FROM runtime_commands" + (" ORDER BY created_at DESC LIMIT 20" if include_finished else " WHERE state='requested' ORDER BY created_at")
            return [dict(row) for row in connection.execute(query)]

    def finish_command(self, identifier, error_type=None):
        with self._connect() as connection:
            connection.execute("UPDATE runtime_commands SET state=?, error_type=? WHERE id=? AND state='requested'",
                               ('completed' if error_type is None else 'failed', error_type, identifier))

    def get_action(self, keeper, account_id, task):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM action_attempts WHERE keeper=? AND account_id=? AND task=?",
                                     (keeper, account_id, task)).fetchone()
            abandoned = self._abandoned(connection, row) if row is not None else None
        if row is None:
            return None
        return ActionAttempt('abandoned' if abandoned is not None else row['state'], row['day'], datetime.fromisoformat(row['started_at']),
                             None if row['confirmed_at'] is None else datetime.fromisoformat(row['confirmed_at']))

    def reserve_action(self, keeper, account_id, task, day, now, *, validate_time=None):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("action day must be an ISO calendar date")
        timestamp = stamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM action_attempts WHERE keeper=? AND account_id=? AND task=?",
                                     (keeper, account_id, task)).fetchone()
            if validate_time is not None:
                validate_time()
            if row is not None:
                abandoned = self._abandoned(connection, row)
                pending_block = row['state'] == 'pending' and (abandoned is None or day < abandoned['business_day'])
                # An archive, including unknown, keeps the old write blocked.
                # Only an unarchived pending with no challenge, answer, or claim can move,
                # on a later day or again on its own day. Re-stamping links the
                # new attempt, so a claim it sends blocks any further retry.
                retryable = (row['state'] == 'pending' and row['day'] <= day and abandoned is None
                             and not self._has_archive(connection, row)
                             and zero_claim_pending_allows_retry(
                                 row['day'], row['state'], self._metadata_for_action(connection, row), day))
                blocked_day = row['day'] > day or (row['day'] == day and not retryable)
                if retryable:
                    pending_block = False
                if blocked_day or pending_block:
                    return False
            connection.execute("""INSERT INTO action_attempts VALUES (?,?,?,'pending',?,?,NULL)
                ON CONFLICT(keeper,account_id,task) DO UPDATE SET
                state='pending', day=excluded.day, started_at=excluded.started_at, confirmed_at=NULL""",
                               (keeper, account_id, task, day, timestamp))
            if validate_time is not None:
                validate_time()
        return True

    def confirm_action(self, keeper, account_id, task, now, *, day, validate_time=None):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("confirmation day must be an ISO calendar date")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if validate_time is not None:
                validate_time()
            row = connection.execute("SELECT * FROM action_attempts WHERE keeper=? AND account_id=? AND task=?",
                                     (keeper, account_id, task)).fetchone()
            if row is not None and connection.execute(
                    'SELECT 1 FROM action_archives WHERE history_id=?',
                    (self._history_id(connection, row),)).fetchone() is not None:
                return False
            changed = connection.execute("""UPDATE action_attempts SET state='confirmed', confirmed_at=?
                WHERE keeper=? AND account_id=? AND task=? AND state='pending' AND day=?""",
                                      (stamp(now), keeper, account_id, task, day)).rowcount == 1
            if validate_time is not None:
                validate_time()
            return changed

    def is_paused(self, keeper, account_id, flame_id):
        with self._connect() as connection:
            return connection.execute("""SELECT 1 FROM runtime_pauses
                WHERE keeper=? AND account_id=? AND scope IN ('*',?)""",
                                      (keeper, account_id, flame_id)).fetchone() is not None

    def blocks_execution(self, keeper, account_id, flame_id, *, now):
        """A flame-scoped pause expires the next business day when that run submitted nothing.

        An account-wide pause stays until an explicit resume. An archived pending
        stays stopped too. Status display can keep using is_paused; this method
        is the execution check. Releasing the flame pause does not delete the
        pending action row.
        """
        with self._connect() as connection:
            scopes = [row["scope"] for row in connection.execute(
                """SELECT scope FROM runtime_pauses
                   WHERE keeper=? AND account_id=? AND scope IN ('*', ?)""",
                (keeper, account_id, flame_id))]
            expired = flame_id in scopes and self._zero_claim_run_expired(
                connection, keeper, flame_id, account_id, now)
        if not scopes or "*" in scopes:
            return "*" in scopes
        if expired:
            self.resume(keeper, account_id, flame_id)
            return False
        return True

    def zero_claim_pending_retryable(self, keeper, account_id, task, day) -> bool:
        """Same-day pending that sent nothing may be read or claimed again."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_attempts WHERE keeper=? AND account_id=? AND task=?",
                (keeper, account_id, task)).fetchone()
            if row is None or row["state"] != "pending" or row["day"] != day:
                return False
            if self._has_archive(connection, row) or self._abandoned(connection, row) is not None:
                return False
            metadata = self._metadata_for_action(connection, row)
        return zero_claim_pending_allows_retry(row["day"], row["state"], metadata, day)

    def zero_claim_pending_released(self, keeper, account_id, task, day) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_attempts WHERE keeper=? AND account_id=? AND task=?",
                (keeper, account_id, task)).fetchone()
            if row is None or row["state"] != "pending" or row["day"] >= day:
                return False
            if self._has_archive(connection, row):
                return False
            metadata = self._metadata_for_action(connection, row)
        return zero_claim_pending_allows_new_day(row["day"], row["state"], metadata, day)

    def _has_archive(self, connection, row) -> bool:
        return connection.execute(
            "SELECT 1 FROM action_archives WHERE history_id=?",
            (self._history_id(connection, row),),
        ).fetchone() is not None

    def _metadata_for_action(self, connection, row):
        found = connection.execute(
            "SELECT metadata_json FROM keep_runs WHERE account_id=? AND started_at=?",
            (row["account_id"], row["started_at"])).fetchall()
        if len(found) != 1 or not found[0]["metadata_json"]:
            return None
        try:
            data = json.loads(found[0]["metadata_json"])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _zero_claim_run_expired(self, connection, keeper, flame_id, account_id, now) -> bool:
        row = connection.execute(
            """SELECT started_at, metadata_json, business_target_json FROM keep_runs
               WHERE flame_id=? AND account_id=? ORDER BY started_at DESC LIMIT 1""",
            (flame_id, account_id)).fetchone()
        if row is None or not row["business_target_json"]:
            return False
        try:
            metadata = json.loads(row["metadata_json"] or "null")
            target = json.loads(row["business_target_json"])
        except ValueError:
            return False
        if not isinstance(metadata, dict) or not isinstance(target, dict):
            return False
        day, zone_name = target.get("day"), target.get("timezone")
        if not isinstance(day, str) or not isinstance(zone_name, str):
            return False
        try:
            today = now.astimezone(ZoneInfo(zone_name)).date().isoformat()
        except Exception:
            return False
        if not zero_claim_pending_allows_new_day(day, "pending", metadata, today):
            return False
        return not self._archived_action_blocks_release(
            connection, keeper, account_id, row["started_at"], metadata)

    def _archived_action_blocks_release(self, connection, keeper, account_id, started_at, metadata) -> bool:
        """An unknown or abandoned archive keeps that pending stopped."""
        task = metadata.get("task")
        if isinstance(task, str):
            rows = connection.execute(
                "SELECT * FROM action_attempts WHERE keeper=? AND account_id=? AND task=?",
                (keeper, account_id, task)).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM action_attempts WHERE keeper=? AND account_id=? AND started_at=?",
                (keeper, account_id, started_at)).fetchall()
        return any(row["state"] == "pending" and self._has_archive(connection, row) for row in rows)

    def pause(self, keeper, account_id, scope, reason, now):
        with self._connect() as connection:
            connection.execute("""INSERT INTO runtime_pauses VALUES (?,?,?,?,?)
                ON CONFLICT(keeper,account_id,scope) DO UPDATE SET
                reason=excluded.reason, paused_at=excluded.paused_at""",
                               (keeper, account_id, scope, reason, stamp(now)))

    def resume(self, keeper, account_id, scope):
        # Resume grants inspection, not another write. Never clear action_attempts.
        with self._connect() as connection:
            connection.execute("DELETE FROM runtime_pauses WHERE keeper=? AND account_id=? AND scope=?",
                               (keeper, account_id, scope))

    def pause_reason(self, keeper, account_id, scope):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT reason FROM runtime_pauses WHERE keeper=? AND account_id=? AND scope=?",
                (keeper, account_id, scope)).fetchone()
        return None if row is None else row["reason"]

    def request_login(self, keeper, account_id, flame_id, now):
        identifier = str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""UPDATE login_requests SET state='expired'
                WHERE expires_at<=? AND state IN ('requested','opening','waiting','confirmed','cancelled')""", (stamp(now),))
            connection.execute("INSERT INTO login_requests VALUES (?,?,?,?,'requested',?,?)",
                               (identifier, keeper, account_id, flame_id, stamp(now), stamp(now + timedelta(minutes=15))))
            connection.execute("""INSERT INTO runtime_pauses VALUES (?,?,'*','login_requested',?)
                ON CONFLICT(keeper,account_id,scope) DO UPDATE SET
                reason='login_requested', paused_at=excluded.paused_at""", (keeper, account_id, stamp(now)))
        return identifier

    def request_inspection(self, flame_id, account_id, now):
        with self._connect() as connection:
            connection.execute("UPDATE flame_state SET next_action_at=? WHERE flame_id=? AND account_id=?",
                               (stamp(now), flame_id, account_id))

    def login_requests(self, *, include_finished=False):
        with self._connect() as connection:
            if include_finished:
                return [dict(row) for row in connection.execute("SELECT * FROM login_requests ORDER BY created_at DESC LIMIT 20")]
            return [dict(row) for row in connection.execute("""SELECT * FROM login_requests
                WHERE state IN ('requested','opening','waiting','confirmed','cancelled') ORDER BY created_at""")]

    def transition_login(self, identifier, expected, state):
        with self._connect() as connection:
            return connection.execute("UPDATE login_requests SET state=? WHERE id=? AND state=?",
                                      (state, identifier, expected)).rowcount == 1

    def respond_login(self, identifier, *, confirm, now):
        with self._connect() as connection:
            return connection.execute("""UPDATE login_requests SET state=?
                WHERE id=? AND state='waiting' AND expires_at>?""",
                                      ('confirmed' if confirm else 'cancelled', identifier, stamp(now))).rowcount == 1
