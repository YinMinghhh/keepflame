"""Bounded local run requests addressed to one executing daemon instance.

This queue contains identifiers and outcomes, never browser state or credentials.
The runner lock is owned for the entire daemon lifetime, including shutdown.
"""

import fcntl
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from keepflame.storage.control import stamp
from keepflame.core.models import BusinessTarget


class DispatchError(ValueError):
    pass


class DispatchStorageMixin:
    @staticmethod
    def _initialize_dispatch(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS daemon_owner (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL,
            config_path TEXT NOT NULL, fingerprint TEXT NOT NULL,
            active INTEGER NOT NULL CHECK(active IN (0,1)),
            started_at TEXT NOT NULL, stopped_at TEXT)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS run_requests (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
            flame_id TEXT NOT NULL, keeper TEXT NOT NULL, account_id TEXT NOT NULL,
            config_path TEXT NOT NULL, fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
                ('requested','running','completed','rejected','expired','cancelled','interrupted')),
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            finished_at TEXT, reason TEXT, run_id TEXT, result_status TEXT)""")
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(run_requests)")}
        for name in ("target_day", "target_timezone"):
            if name not in existing:
                connection.execute(f"ALTER TABLE run_requests ADD COLUMN {name} TEXT")
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_pending_run_request
            ON run_requests(keeper, account_id, flame_id)
            WHERE state IN ('requested','running')""")

    @staticmethod
    def _lock_held(path):
        try:
            handle = path.open('r')
        except FileNotFoundError:
            return False
        with handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def runner_active(self):
        """Any executing runner, including standalone run-once; not daemon proof."""
        return self._lock_held(Path(str(self.path.resolve()) + '.runner.lock'))

    def _daemon_lock_path(self, owner_id):
        try:
            if str(UUID(owner_id)) != owner_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise DispatchError('invalid daemon owner ID') from None
        return Path(str(self.path.resolve()) + f'.daemon-{owner_id}.lock')

    def daemon_owner_active(self, owner_id):
        """Snapshot of this exact daemon generation's process-owned lock."""
        try:
            path = self._daemon_lock_path(owner_id)
        except DispatchError:
            return False
        return self._lock_held(path)

    @contextmanager
    def daemon_owner_lock(self, owner_id):
        # Called inside execution_lock. Unique generation names prevent an old
        # owner record from borrowing another daemon's or standalone runner's lock.
        path = self._daemon_lock_path(owner_id)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _retire_requests(connection, now, reason):
        # A started run may have acted before it lost its acknowledgement.
        # Preserve its action ledger and pause inspection rather than replay it.
        connection.execute("""INSERT INTO runtime_pauses(keeper,account_id,scope,reason,paused_at)
            SELECT keeper,account_id,flame_id,'daemon_run_interrupted',?
            FROM run_requests WHERE state='running'
            ON CONFLICT(keeper,account_id,scope) DO NOTHING""", (stamp(now),))
        connection.execute("""UPDATE run_requests SET
            state=CASE state WHEN 'running' THEN 'interrupted' ELSE 'cancelled' END,
            reason=?, finished_at=? WHERE state IN ('requested','running')""", (reason, stamp(now)))

    def start_dispatch(self, owner_id, config, now):
        # Caller must hold execution_lock; a new generation never takes old work.
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._retire_requests(connection, now, 'owner_restarted')
            connection.execute("""INSERT INTO daemon_owner VALUES (1,?,?,?,1,?,NULL)
                ON CONFLICT(singleton) DO UPDATE SET id=excluded.id,
                config_path=excluded.config_path, fingerprint=excluded.fingerprint,
                active=1, started_at=excluded.started_at, stopped_at=NULL""",
                (owner_id, str(config.source_path.resolve()), config.source_fingerprint, stamp(now)))

    def refresh_dispatch(self, owner_id, config):
        with self._connect() as connection:
            connection.execute("UPDATE daemon_owner SET fingerprint=? WHERE id=? AND active=1",
                               (config.source_fingerprint, owner_id))

    def stop_dispatch(self, owner_id, now):
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            changed = connection.execute("UPDATE daemon_owner SET active=0, stopped_at=? WHERE id=? AND active=1",
                                         (stamp(now), owner_id)).rowcount
            if changed:
                self._retire_requests(connection, now, 'owner_stopped')

    def submit_run(self, identifier, config, flame, now):
        try:
            if str(UUID(identifier)) != identifier:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise DispatchError('--request-id must be a canonical UUID') from None
        identity = (flame.id, flame.keeper, flame.account_id,
                    str(config.source_path.resolve()), config.source_fingerprint)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('SELECT * FROM run_requests WHERE id=?', (identifier,)).fetchone()
            if previous is not None:
                if tuple(previous[k] for k in ('flame_id','keeper','account_id','config_path','fingerprint')) != identity:
                    raise DispatchError('request ID already belongs to another target or configuration')
                return dict(previous)
            owner = connection.execute('SELECT * FROM daemon_owner WHERE singleton=1').fetchone()
            if (owner is None or not owner['active'] or not self.runner_active()
                    or not self.daemon_owner_active(owner['id'])):
                raise DispatchError('no executing daemon; request was not queued')
            if (owner['config_path'], owner['fingerprint']) != identity[3:]:
                raise DispatchError('daemon configuration differs; explicit reload is required')
            connection.execute("""UPDATE run_requests SET state='expired', reason='expired', finished_at=?
                WHERE state='requested' AND expires_at<=?""", (stamp(now), stamp(now)))
            pending = connection.execute("SELECT COUNT(*) FROM run_requests WHERE state IN ('requested','running')").fetchone()[0]
            if pending >= 16:
                raise DispatchError('daemon request queue is full')
            if connection.execute("""SELECT 1 FROM run_requests WHERE keeper=? AND account_id=? AND flame_id=?
                AND state IN ('requested','running')""", (flame.keeper, flame.account_id, flame.id)).fetchone():
                raise DispatchError('this Flame already has an unfinished request; reuse its request ID')
            target = BusinessTarget.at(now, flame.schedule.timezone)
            connection.execute("""INSERT INTO run_requests
                (id,owner_id,flame_id,keeper,account_id,config_path,fingerprint,state,created_at,expires_at,
                 target_day,target_timezone) VALUES (?,?,?,?,?,?,?,'requested',?,?,?,?)""",
                (identifier, owner['id'], *identity, stamp(now), stamp(now + timedelta(minutes=5)),
                 target.day, target.timezone))
            return dict(connection.execute('SELECT * FROM run_requests WHERE id=?', (identifier,)).fetchone())

    def claim_run(self, owner_id, now):
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not connection.execute('SELECT 1 FROM daemon_owner WHERE id=? AND active=1', (owner_id,)).fetchone():
                return None
            connection.execute("""UPDATE run_requests SET state='expired', reason='expired', finished_at=?
                WHERE owner_id=? AND state='requested' AND expires_at<=?""", (stamp(now), owner_id, stamp(now)))
            row = connection.execute("""SELECT * FROM run_requests WHERE owner_id=? AND state='requested'
                ORDER BY created_at, rowid LIMIT 1""", (owner_id,)).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE run_requests SET state='running' WHERE id=?", (row['id'],))
            return dict(row)

    def finish_run_request(self, identifier, owner_id, now, *, reason=None, outcome=None):
        with self._connect() as connection:
            connection.execute("""UPDATE run_requests SET state=?, finished_at=?, reason=?, run_id=?, result_status=?
                WHERE id=? AND owner_id=? AND state='running'""",
                ('rejected' if reason else 'completed', stamp(now), reason,
                 None if outcome is None else outcome.record.run_id,
                 None if outcome is None else outcome.record.result.status.value, identifier, owner_id))

    def has_requested_run(self, owner_id, flame):
        with self._connect() as connection:
            return connection.execute("""SELECT 1 FROM run_requests
                WHERE owner_id=? AND flame_id=? AND keeper=? AND account_id=? AND state='requested'""",
                (owner_id, flame.id, flame.keeper, flame.account_id)).fetchone() is not None

    def dispatch_status(self):
        with self._connect() as connection:
            owner = connection.execute('SELECT * FROM daemon_owner WHERE singleton=1').fetchone()
            requests = [dict(row) for row in connection.execute('SELECT * FROM run_requests ORDER BY created_at DESC, rowid DESC LIMIT 20')]
        return {'owner_record': None if owner is None else dict(owner),
                'runner_lock_held': self.runner_active(),
                'daemon_owner_lock_held': owner is not None and self.daemon_owner_active(owner['id']),
                'requests': requests}
