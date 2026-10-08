"""Append-only action snapshots and explicit, evidence-bound dispositions."""
from datetime import date, datetime, timezone
import hashlib
import json
import re
from uuid import UUID
from zoneinfo import ZoneInfo


from keepflame.core.protocols import ArchiveError


FIELDS = ('keeper', 'account_id', 'task', 'state', 'day', 'started_at', 'confirmed_at')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def evidence_references(evidence):
    if not isinstance(evidence, dict) or set(evidence) != {'version', 'source_revision', 'references'}:
        raise ArchiveError('archive evidence requires only version, source_revision and references')
    if type(evidence['version']) is not int or evidence['version'] != 1:
        raise ArchiveError('unsupported evidence version')
    if not isinstance(evidence['source_revision'], str) or not re.fullmatch('[0-9a-f]{40}', evidence['source_revision']):
        raise ArchiveError('source_revision must pin a reviewed full commit')
    refs = evidence['references']
    if not isinstance(refs, list) or not 1 <= len(refs) <= 8 or len(set(refs)) != len(refs):
        raise ArchiveError('one to eight distinct evidence digests required')
    if any(not isinstance(ref, str) or not re.fullmatch('[0-9a-f]{64}', ref) for ref in refs):
        raise ArchiveError('evidence references must be SHA256 digests, not raw data')
    return canonical(evidence)


class ArchiveStorageMixin:
    @staticmethod
    def _initialize_archive(connection):
        connection.execute('''CREATE TABLE IF NOT EXISTS action_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keeper TEXT NOT NULL, account_id TEXT NOT NULL, task TEXT NOT NULL,
            state TEXT NOT NULL, day TEXT NOT NULL, started_at TEXT NOT NULL, confirmed_at TEXT)''')
        columns = ','.join(FIELDS)
        match = ' AND '.join('h.'+f+' IS a.'+f for f in FIELDS)
        connection.execute(f'''INSERT INTO action_history ({columns}) SELECT {columns} FROM action_attempts a
            WHERE NOT EXISTS(SELECT 1 FROM action_history h WHERE {match})''')
        for event in ('INSERT', 'UPDATE'):
            connection.execute(f'''CREATE TRIGGER IF NOT EXISTS snapshot_action_{event.lower()}
                AFTER {event} ON action_attempts BEGIN
                INSERT INTO action_history ({columns}) VALUES ({','.join('NEW.'+f for f in FIELDS)}); END''')
        connection.execute('''CREATE TRIGGER IF NOT EXISTS retain_action_intent BEFORE DELETE ON action_attempts
            BEGIN SELECT RAISE(ABORT, 'action intents cannot be deleted'); END''')
        connection.execute('''CREATE TABLE IF NOT EXISTS action_archives (
            id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
            history_id INTEGER NOT NULL REFERENCES action_history(id),
            disposition TEXT NOT NULL CHECK(disposition IN ('unknown','abandoned')),
            business_day TEXT NOT NULL, archived_at TEXT NOT NULL,
            flame_id TEXT NOT NULL, run_id TEXT, run_snapshot_json TEXT,
            pause_snapshot_json TEXT NOT NULL, evidence_json TEXT NOT NULL)''')
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_abandonment ON action_archives(history_id) WHERE disposition='abandoned'")
        for table in ('action_history', 'action_archives'):
            for event in ('UPDATE', 'DELETE'):
                connection.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{event.lower()}
                    BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT, 'action audit is immutable'); END''')

    @staticmethod
    def _history_id(connection, row):
        match = ' AND '.join(field+' IS ?' for field in FIELDS)
        found = connection.execute('SELECT id FROM action_history WHERE '+match+' ORDER BY id DESC LIMIT 1',
                                   tuple(row[field] for field in FIELDS)).fetchone()
        if found is None:
            raise ArchiveError('action snapshot is missing')
        return found['id']

    def _abandoned(self, connection, row):
        if row['state'] != 'pending':
            return None
        history_id = self._history_id(connection, row)
        return connection.execute("SELECT * FROM action_archives WHERE history_id=? AND disposition='abandoned'",
                                  (history_id,)).fetchone()

    def action_audit(self, keeper, account_id, task):
        with self._connect() as connection:
            history = [dict(row) for row in connection.execute('''SELECT * FROM action_history
                WHERE keeper=? AND account_id=? AND task=? ORDER BY id''', (keeper, account_id, task))]
            archives = [dict(row) for row in connection.execute('''SELECT r.* FROM action_archives r
                JOIN action_history h ON h.id=r.history_id WHERE h.keeper=? AND h.account_id=? AND h.task=?
                ORDER BY r.archived_at,r.id''', (keeper, account_id, task))]
        return {'history': history, 'archives': archives}

    def archive_action(self, *, request_id, flame, day, started_at, disposition, evidence, now, run_id=None, review=None):
        if str(UUID(request_id)) != request_id or disposition not in ('unknown', 'abandoned'):
            raise ArchiveError('canonical request UUID and supported disposition required')
        if date.fromisoformat(day).isoformat() != day:
            raise ArchiveError('original action day must be canonical')
        if now.tzinfo is None or now.utcoffset() is None or started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ArchiveError('archive timestamps must be aware')
        expected_start = started_at.astimezone(timezone.utc).isoformat()
        task = flame.settings.get('task', 'checkin')
        data = evidence_references(evidence)
        identity = (flame.keeper, flame.account_id, task)
        request_hash = hashlib.sha256(canonical([*identity, flame.id, day, expected_start, disposition,
                                                 run_id, json.loads(data)]).encode()).hexdigest()
        business_day = now.astimezone(ZoneInfo(flame.schedule.timezone)).date().isoformat()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('SELECT * FROM action_archives WHERE id=?', (request_id,)).fetchone()
            if previous is not None:
                if previous['request_hash'] != request_hash:
                    raise ArchiveError('archive request UUID is bound to different evidence or identity')
                return dict(previous)
            row = connection.execute('SELECT * FROM action_attempts WHERE keeper=? AND account_id=? AND task=?', identity).fetchone()
            if (row is None or row['state'] != 'pending' or row['day'] != day or row['started_at'] != expected_start
                    or day >= business_day or started_at > now):
                raise ArchiveError('only the exact expired pending intent can be archived')
            history_id = self._history_id(connection, row)
            if connection.execute("SELECT 1 FROM action_archives WHERE history_id=? AND disposition='abandoned'", (history_id,)).fetchone():
                raise ArchiveError('intent already abandoned by another archive request')
            run = None
            if run_id is not None:
                run = connection.execute('SELECT * FROM keep_runs WHERE run_id=?', (run_id,)).fetchone()
                if (run is None or run['account_id'] != flame.account_id or run['flame_id'] != flame.id
                        or run['started_at'] != row['started_at']
                        or datetime.fromisoformat(run['finished_at']) > now):
                    raise ArchiveError('run evidence does not identify this exact intent')
            if disposition == 'abandoned':
                if run is None or review is None:
                    raise ArchiveError('abandonment requires a recorded run and platform evidence review')
                review(dict(row), dict(run), evidence)
            pauses = [dict(pause) for pause in connection.execute(
                "SELECT * FROM runtime_pauses WHERE keeper=? AND account_id=? AND scope IN ('*',?) ORDER BY scope",
                (flame.keeper, flame.account_id, flame.id))]
            connection.execute('INSERT INTO action_archives VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (request_id, request_hash, history_id, disposition, business_day, now.astimezone(timezone.utc).isoformat(),
                 flame.id, run_id, None if run is None else canonical(dict(run)), canonical(pauses), data))
            # No action row, success record, pause or schedule is modified here.
            return dict(connection.execute('SELECT * FROM action_archives WHERE id=?', (request_id,)).fetchone())
