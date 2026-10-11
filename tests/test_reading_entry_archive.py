"""Synthetic historical-case binding and real SQLite transaction boundaries."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from keepflame.core.protocols import ArchiveError
from keepflame.flames.joinquant import archive_cases
from keepflame.flames.joinquant.archive import review_abandonment
from keepflame.storage.archive import canonical
from tests.test_action_archive import COUNTS, NOW, OLD, setup


def pin(monkeypatch, action, run, evidence):
    case = archive_cases.ReadingEntryCase(
        archive_cases.intent_sha256(action), archive_cases.run_id_sha256(run),
        evidence['references'][0],
        archive_cases.snapshot_sha256(action), archive_cases.snapshot_sha256(run),
        evidence['source_revision'], frozenset(evidence['references']))
    monkeypatch.setattr(archive_cases, 'REVIEWED_READING_ENTRY_CASES', (case,))


@pytest.fixture
def case(tmp_path):
    store, flame, args = setup(tmp_path, schema=1)
    with store._connect() as con:
        action = dict(con.execute('SELECT * FROM action_attempts').fetchone())
        run = dict(con.execute('SELECT * FROM keep_runs').fetchone())
        metadata = json.loads(run['metadata_json'])
        metadata.update(reason='reading_button_changed', action_stage='claim',
                        business_target={'day': action['day'], 'timezone': 'Asia/Shanghai'})
        con.execute('UPDATE keep_runs SET metadata_json=?,business_target_json=?',
                    (canonical(metadata), canonical(metadata['business_target'])))
        con.execute("UPDATE runtime_pauses SET reason='reading_button_changed'")
        run = dict(con.execute('SELECT * FROM keep_runs').fetchone())
    args['evidence'] = {'version': 1, 'source_revision': 'b' * 40,
                        'references': ['c' * 64, 'd' * 64]}
    return store, flame, args, action, run


def test_zero_counters_and_schema_alone_are_not_reviewed_case(case):
    store, _, args, _, _ = case
    with pytest.raises(ArchiveError, match='exact reviewed intent'):
        store.archive_action(**args)
    assert store.get_action('joinquant', 'primary', 'reading').state == 'pending'
    assert store.action_audit('joinquant', 'primary', 'reading')['archives'] == []
    assert store.is_paused('joinquant', 'primary', 'reading')


def test_known_case_is_append_only_and_pause_is_separate(case, monkeypatch):
    store, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    with store._connect() as con:
        tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0] != 'action_archives']
        before = {name: [tuple(row) for row in con.execute('SELECT * FROM ' + name)] for name in tables}
    saved = store.archive_action(**args)
    assert saved['disposition'] == 'abandoned'
    assert json.loads(saved['run_snapshot_json']) == run
    assert json.loads(saved['pause_snapshot_json'])[0]['reason'] == 'reading_button_changed'
    assert store.archive_action(**args) == saved
    with store._connect() as con:
        assert {name: [tuple(row) for row in con.execute('SELECT * FROM ' + name)] for name in tables} == before
    assert store.get_action('joinquant', 'primary', 'reading').state == 'abandoned'
    assert store.is_paused('joinquant', 'primary', 'reading')
    assert not store.confirm_action('joinquant', 'primary', 'reading', OLD, day=action['day'])
    assert not store.reserve_action('joinquant', 'primary', 'reading', action['day'], NOW)
    assert store.reserve_action('joinquant', 'primary', 'reading', '2026-10-09', NOW)
    new = store.get_action('joinquant', 'primary', 'reading')
    with pytest.raises(ArchiveError):
        store.archive_action(**{**args, 'request_id': str(uuid4())})
    assert store.get_action('joinquant', 'primary', 'reading') == new


def test_unknown_keeps_budget_and_same_day_never_abandons(case, monkeypatch):
    store, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    with pytest.raises(ArchiveError, match='expired pending'):
        store.archive_action(**{**args, 'now': OLD})
    assert store.action_audit('joinquant', 'primary', 'reading')['archives'] == []
    unknown = store.archive_action(**{**args, 'disposition': 'unknown', 'review': None})
    assert unknown['disposition'] == 'unknown'
    assert store.get_action('joinquant', 'primary', 'reading').state == 'pending'
    assert not store.reserve_action('joinquant', 'primary', 'reading', '2026-10-09', NOW)
    assert store.is_paused('joinquant', 'primary', 'reading')


@pytest.mark.parametrize('row,key,value', [
    ('action', 'keeper', 'another'), ('action', 'account_id', 'another'),
    ('action', 'task', 'checkin'), ('action', 'day', '2026-10-08'),
    ('action', 'started_at', '2026-10-07T15:03:01+00:00'),
    ('action', 'state', 'confirmed'), ('action', 'confirmed_at', OLD.isoformat()),
    ('run', 'run_id', 'another'), ('run', 'flame_id', 'another'),
    ('run', 'account_id', 'another'), ('run', 'started_at', '2026-10-07T15:03:01+00:00'),
    ('run', 'finished_at', '2026-10-07T15:05:00+00:00'),
    ('run', 'business_target_json', '{"day":"2026-10-08","timezone":"Asia/Shanghai"}'),
    ('run', 'status', 'WAITING_HUMAN'), ('run', 'reward_json', '{}'),
    ('run', 'completion_receipt_json', '{}'), ('run', 'clock_regressed', 1),
    ('run', 'human_action_json', '{}'), ('run', 'next_action_at', None),
    ('run', 'message', 'different evidence'),
])
def test_full_intent_and_run_snapshot_must_match(case, monkeypatch, row, key, value):
    _, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    mutated = {'action': deepcopy(action), 'run': deepcopy(run)}
    mutated[row][key] = value
    with pytest.raises(ArchiveError):
        review_abandonment(mutated['action'], mutated['run'], args['evidence'])


@pytest.mark.parametrize('key,value', [
    ('version', True), ('source_revision', 'e' * 40),
    ('references', []), ('references', ['c' * 64]),
    ('references', ['c' * 64, 'd' * 64, 'e' * 64]),
    ('references', ['c' * 64, 'e' * 64]),
    ('references', ['c' * 64, 'd' * 64, 'd' * 64]),
    ('references', ['c' * 64, {}]), ('references', None),
])
def test_source_and_exact_evidence_set_are_mandatory(case, monkeypatch, key, value):
    _, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    with pytest.raises(ArchiveError):
        review_abandonment(action, run, {**args['evidence'], key: value})


def test_reference_order_does_not_change_case_and_extra_fields_are_rejected(case, monkeypatch):
    _, _, args, action, run = case
    evidence = args['evidence']
    pin(monkeypatch, action, run, evidence)
    review_abandonment(action, run, {**evidence, 'references': list(reversed(evidence['references']))})
    with pytest.raises(ArchiveError):
        review_abandonment(action, run, {**evidence, 'authorization': True})


@pytest.mark.parametrize('counter,value', [(key, value) for key in COUNTS
                                         for value in (1, -1, True, 0.0, '0', None)])
def test_even_pinned_case_cannot_override_strict_zero_write_rule(case, monkeypatch, counter, value):
    _, _, args, action, run = case
    metadata = json.loads(run['metadata_json'])
    metadata['stage_counts'][counter] = value
    run['metadata_json'] = canonical(metadata)
    pin(monkeypatch, action, run, args['evidence'])
    with pytest.raises(ArchiveError, match='unadmitted'):
        review_abandonment(action, run, args['evidence'])


@pytest.mark.parametrize('field,value', [('write_guard_schema', None), ('write_guard_schema', True),
    ('write_guard_schema', 0), ('execution_mode', 'inspect'), ('action_stage', 'reading'),
    ('automation_paused', False), ('task', 'checkin'), ('stage_counts', {})])
def test_even_pinned_case_requires_guard_schema_and_failure_semantics(case, monkeypatch, field, value):
    _, _, args, action, run = case
    metadata = json.loads(run['metadata_json'])
    metadata[field] = value
    run['metadata_json'] = canonical(metadata)
    pin(monkeypatch, action, run, args['evidence'])
    with pytest.raises(ArchiveError):
        review_abandonment(action, run, args['evidence'])


def test_concurrent_case_archive_is_idempotent(case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from keepflame.storage import SQLiteStorage
    store, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: SQLiteStorage(store.path).archive_action(**args), range(2)))
    assert results[0] == results[1]
    assert len(store.action_audit('joinquant', 'primary', 'reading')['archives']) == 1
    assert store.is_paused('joinquant', 'primary', 'reading')


LEGACY_REASONS = ('unexpected_write', 'business_day_changed', 'article_navigation_changed',
                  'article_not_public', 'reading_registration_unconfirmed')


def changed_reason(run, reason):
    changed = deepcopy(run)
    metadata = json.loads(changed['metadata_json'])
    metadata['reason'] = reason
    changed['metadata_json'] = canonical(metadata)
    return changed


def evidence_variant(evidence, kind):
    if kind == 'original':
        return evidence
    if kind == 'replaced':
        return {'version': 1, 'source_revision': '0' * 40, 'references': ['1' * 64]}
    return {'version': 1, 'source_revision': evidence['source_revision']}


def table_snapshot(store):
    with store._connect() as con:
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: [tuple(row) for row in con.execute('SELECT * FROM ' + name)] for name in names}


@pytest.mark.parametrize('reason', LEGACY_REASONS)
@pytest.mark.parametrize('kind', ['original', 'replaced', 'missing'])
@pytest.mark.parametrize('entry', ['predicate', 'keeper', 'storage'])
def test_known_reason_cannot_fall_back_through_any_review_entry(case, monkeypatch, reason, kind, entry):
    from keepflame.flames.joinquant.keeper import JoinQuantKeeper
    store, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    changed = changed_reason(run, reason)
    evidence = evidence_variant(args['evidence'], kind)
    with store._connect() as con:
        con.execute('UPDATE keep_runs SET metadata_json=? WHERE run_id=?',
                    (changed['metadata_json'], run['run_id']))
    before = table_snapshot(store)
    with pytest.raises(ArchiveError):
        if entry == 'storage':
            store.archive_action(**{**args, 'evidence': evidence})
        elif entry == 'keeper':
            JoinQuantKeeper.review_abandonment(action, changed, evidence)
        else:
            review_abandonment(action, changed, evidence)
    assert table_snapshot(store) == before
    assert store.get_action('joinquant', 'primary', 'reading').state == 'pending'
    assert store.is_paused('joinquant', 'primary', 'reading')
    assert store.action_audit('joinquant', 'primary', 'reading')['archives'] == []
    assert store.reserve_action('joinquant', 'primary', 'reading', '2026-10-09', NOW)
    assert store.get_action('joinquant', 'primary', 'reading').day == '2026-10-09'


@pytest.mark.parametrize('row,key,value', [
    ('action', 'keeper', 'another'), ('action', 'account_id', 'another'),
    ('action', 'task', 'checkin'), ('action', 'day', '2026-10-08'),
    ('action', 'started_at', '2026-10-07T15:03:01+00:00'),
    ('action', 'state', 'confirmed'), ('action', 'confirmed_at', OLD.isoformat()),
    ('run', 'run_id', 'another'), ('run', 'metadata_json', '{broken'),
])
def test_independent_identity_anchors_survive_row_changes(case, monkeypatch, row, key, value):
    _, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    changed = {'action': deepcopy(action), 'run': changed_reason(run, 'unexpected_write')}
    changed[row][key] = value
    with pytest.raises(ArchiveError):
        review_abandonment(changed['action'], changed['run'], evidence_variant(args['evidence'], 'replaced'))


def test_case_certificate_cannot_be_reused_by_unrelated_legacy_identity(case, monkeypatch):
    _, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    action = {**action, 'account_id': 'unrelated'}
    run = {**changed_reason(run, 'unexpected_write'), 'run_id': str(uuid4()), 'account_id': 'unrelated'}
    with pytest.raises(ArchiveError):
        review_abandonment(action, run, args['evidence'])


@pytest.mark.parametrize('reason', LEGACY_REASONS)
@pytest.mark.parametrize('schema', [None, 1])
def test_unrelated_ordinary_case_keeps_original_archive_contract(case, tmp_path, monkeypatch, reason, schema):
    from datetime import timedelta
    _, _, known_args, known_action, known_run = case
    pin(monkeypatch, known_action, known_run, known_args['evidence'])
    other = tmp_path / 'ordinary'
    other.mkdir()
    store, _, args = setup(other, schema=schema)
    # Same account/task/day, but a different reservation identity and run UUID.
    start = OLD + timedelta(seconds=1)
    with store._connect() as con:
        run = dict(con.execute('SELECT * FROM keep_runs').fetchone())
        changed = changed_reason(run, reason)
        con.execute('UPDATE action_attempts SET started_at=?', (start.isoformat(),))
        con.execute('UPDATE keep_runs SET started_at=?,finished_at=?,metadata_json=?',
                    (start.isoformat(), start.isoformat(), changed['metadata_json']))
    evidence = args['evidence'] if schema is None else evidence_variant(args['evidence'], 'replaced')
    args.update(started_at=start, evidence=evidence)
    before = table_snapshot(store)
    result = store.archive_action(**args)
    after = table_snapshot(store)
    assert result['disposition'] == 'abandoned'
    assert {k: v for k, v in before.items() if k != 'action_archives'} == {
        k: v for k, v in after.items() if k != 'action_archives'}
    assert store.is_paused('joinquant', 'primary', 'reading')


@pytest.mark.parametrize('kind', ['original', 'replaced', 'missing'])
def test_cli_uses_the_same_reason_independent_review(case, tmp_path, monkeypatch, capsys, kind):
    from datetime import datetime
    from keepflame.cli import main
    from tests.test_config import BASE_CONFIG, write_config
    store, _, args, action, run = case
    pin(monkeypatch, action, run, args['evidence'])
    changed = changed_reason(run, 'unexpected_write')
    with store._connect() as con:
        con.execute('UPDATE keep_runs SET metadata_json=? WHERE run_id=?',
                    (changed['metadata_json'], run['run_id']))
    path = write_config(tmp_path, BASE_CONFIG.replace('id = "joinquant-primary"', 'id = "reading"').replace(
        '[flames.settings]', '[flames.settings]\ntask="reading"'))
    evidence = tmp_path / 'case-evidence.json'
    evidence.write_text(json.dumps(evidence_variant(args['evidence'], kind)))
    monkeypatch.setattr('keepflame.cli.build_runtime', lambda *a, **kw: pytest.fail('runtime forbidden'))
    class Time:
        now = staticmethod(lambda tz: NOW)
        fromisoformat = staticmethod(datetime.fromisoformat)
    monkeypatch.setattr('keepflame.cli.datetime', Time)
    before = table_snapshot(store)
    result = main(['archive-action', '--execute', '--config', str(path), '--flame', 'reading',
                   '--day', action['day'], '--started-at', action['started_at'], '--disposition', 'abandoned',
                   '--request-id', args['request_id'], '--run-id', run['run_id'], '--evidence', str(evidence)])
    assert result != 0
    capsys.readouterr()
    assert table_snapshot(store) == before
    assert store.get_action('joinquant', 'primary', 'reading').state == 'pending'
