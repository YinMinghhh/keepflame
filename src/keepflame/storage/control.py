"""Durable write intents, operator pauses and local login requests.

Only identifiers and normalized states are stored here, never browser material.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

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
        if row is None:
            return None
        return ActionAttempt(row['state'], row['day'], datetime.fromisoformat(row['started_at']),
                             None if row['confirmed_at'] is None else datetime.fromisoformat(row['confirmed_at']))

    def reserve_action(self, keeper, account_id, task, day, now):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("action day must be an ISO calendar date")
        timestamp = stamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, day FROM action_attempts WHERE keeper=? AND account_id=? AND task=?",
                                     (keeper, account_id, task)).fetchone()
            if row is not None and (row['state'] == 'pending' or row['day'] >= day):
                return False
            connection.execute("""INSERT INTO action_attempts VALUES (?,?,?,'pending',?,?,NULL)
                ON CONFLICT(keeper,account_id,task) DO UPDATE SET
                state='pending', day=excluded.day, started_at=excluded.started_at, confirmed_at=NULL""",
                               (keeper, account_id, task, day, timestamp))
        return True

    def confirm_action(self, keeper, account_id, task, now, *, day):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("confirmation day must be an ISO calendar date")
        with self._connect() as connection:
            return connection.execute("""UPDATE action_attempts SET state='confirmed', confirmed_at=?
                WHERE keeper=? AND account_id=? AND task=? AND state='pending' AND day=?""",
                                      (stamp(now), keeper, account_id, task, day)).rowcount == 1

    def is_paused(self, keeper, account_id, flame_id):
        with self._connect() as connection:
            return connection.execute("""SELECT 1 FROM runtime_pauses
                WHERE keeper=? AND account_id=? AND scope IN ('*',?)""",
                                      (keeper, account_id, flame_id)).fetchone() is not None

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
