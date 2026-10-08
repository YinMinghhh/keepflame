"""Real SQLite archive decisions, immutable snapshots and explicit CLI defaults."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from keepflame.cli import main
from keepflame.core.config import load_config
from keepflame.core.models import KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.protocols import ArchiveError
from keepflame.flames.joinquant.archive import review_abandonment
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper
from keepflame.flames.joinquant.task import task_from_settings
from keepflame.storage import SQLiteStorage
from keepflame.storage.sqlite import StorageError
from tests.helpers import FixedClock, FakeNotifier, StaticRegistry, StaticSecrets
from tests.test_config import write_config
from tests.test_joinquant_reading import reading

OLD = datetime(2026,10,7,15,3,tzinfo=timezone.utc)
NOW = OLD + timedelta(days=2)
REFS = {'version':1, 'source_revision':'acbe79bc24d26cf5740dacb588b8605604007510', 'references':['a'*64]}
COUNTS = dict(reading_registration=0, challenge=0, answer=0, claim=0)


def setup(tmp_path, *, counts=None, schema=None):
    config = load_config(write_config(tmp_path))
    flame = replace(config.flames[0], id='reading', settings={'task':'reading','article_url':'https://www.joinquant.com/view/community/detail/12345'})
    store = SQLiteStorage(config.storage.path)
    class Keeper:
        def keep(self, context):
            assert context.storage.reserve_action('joinquant','primary','reading','2026-10-07',context.now)
            metadata = {'task':'reading','execution_mode':'browser','action_stage':'reading','reason':'unexpected_write',
                        'automation_paused':True,'stage_counts':COUNTS if counts is None else counts}
            if schema is not None:metadata['write_guard_schema']=schema
            return KeepResult(KeepStatus.FAILED,'synthetic stopped before writes',metadata=metadata)
    runner = Orchestrator(storage=store,registry=StaticRegistry(Keeper()),capabilities=None,
                          secrets=StaticSecrets('unused'),notifier=FakeNotifier(),clock=FixedClock(OLD))
    outcome = runner.run_flame(flame,notify=False)
    options = dict(request_id=str(uuid4()),flame=flame,day='2026-10-07',started_at=OLD,
                   disposition='abandoned',evidence=REFS,now=NOW,run_id=outcome.record.run_id,review=review_abandonment)
    return store,flame,options


def test_unknown_then_abandoned_retains_original_and_only_new_dates_can_reserve(tmp_path):
    store,flame,args = setup(tmp_path)
    original = store.get_action('joinquant','primary','reading')
    unknown = store.archive_action(**{**args,'disposition':'unknown','run_id':None,'review':None})
    assert unknown['disposition']=='unknown' and store.get_action('joinquant','primary','reading')==original
    assert not store.reserve_action('joinquant','primary','reading','2026-10-09',NOW)
    assert not store.confirm_action('joinquant','primary','reading',OLD,day='2026-10-07')
    decided = store.archive_action(**{**args,'request_id':str(uuid4())})
    assert decided['disposition']=='abandoned'
    assert json.loads(decided['pause_snapshot_json'])[0]['reason']=='unexpected_write'
    assert store.get_action('joinquant','primary','reading').state=='abandoned'
    with store._connect() as c:
        raw=dict(c.execute('SELECT * FROM action_attempts').fetchone())
    assert raw['state']=='pending' and raw['day']=='2026-10-07' and raw['confirmed_at'] is None
    assert store.is_paused('joinquant','primary','reading')
    assert not store.confirm_action('joinquant','primary','reading',OLD,day='2026-10-07')
    for day in ('2026-10-06','2026-10-07','2026-10-08'):
        assert not store.reserve_action('joinquant','primary','reading',day,NOW)
    assert store.reserve_action('joinquant','primary','reading','2026-10-09',NOW)
    audit=store.action_audit('joinquant','primary','reading')
    assert len(audit['archives'])==2 and len(audit['history'])==2
    assert {k:v for k,v in audit['history'][0].items() if k!='id'}==raw
    assert store.get_action('joinquant','primary','reading').day=='2026-10-09'
    assert store.count_runs()==1 and store.is_paused('joinquant','primary','reading')
    assert store.archive_action(**{**args,'request_id':decided['id']})==decided


@pytest.mark.parametrize('key,value', [(key,v) for key in COUNTS for v in (1,True,-1,'0',None)])
def test_nonzero_ambiguous_or_missing_write_evidence_cannot_abandon(tmp_path,key,value):
    store,_,args=setup(tmp_path,counts={**COUNTS,key:value})
    with pytest.raises(ArchiveError):store.archive_action(**args)
    assert store.get_action('joinquant','primary','reading').state=='pending'
    assert store.action_audit('joinquant','primary','reading')['archives']==[]


@pytest.mark.parametrize('field,value', [('day','2026-10-06'),('started_at',OLD+timedelta(seconds=1)),
    ('run_id','missing'),('now',OLD),('review',None)])
def test_archive_matches_exact_expired_intent_and_recorded_run(tmp_path,field,value):
    store,_,args=setup(tmp_path)
    with pytest.raises(ArchiveError):store.archive_action(**{**args,field:value})
    assert store.action_audit('joinquant','primary','reading')['archives']==[]


def test_unsupported_source_and_boolean_schema_are_not_proof(tmp_path):
    store,_,args=setup(tmp_path,schema=True)
    with pytest.raises(ArchiveError):store.archive_action(**{**args,'evidence':{**REFS,'source_revision':'b'*40}})


def test_idempotency_conflict_and_audit_cannot_be_rewritten(tmp_path):
    store,_,args=setup(tmp_path)
    original=store.archive_action(**args)
    assert store.archive_action(**args)==original
    with pytest.raises(ArchiveError):store.archive_action(**{**args,'disposition':'unknown'})
    with pytest.raises(ArchiveError):store.archive_action(**{**args,'request_id':str(uuid4())})
    for sql in ('UPDATE action_history SET day="2026-10-09"','DELETE FROM action_history',
                'UPDATE action_archives SET disposition="unknown"','DELETE FROM action_archives','DELETE FROM action_attempts'):
        with pytest.raises(sqlite3.IntegrityError):
            with store._connect() as c:c.execute(sql)
    assert store.archive_action(**args)==original


def test_v4_migration_preserves_all_existing_rows_and_blocks_old_client(tmp_path):
    store,_,_=setup(tmp_path)
    with store._connect() as c:
        for row in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            c.execute('DROP TRIGGER '+row['name'])
        c.execute('DROP TABLE action_archives');c.execute('DROP TABLE action_history')
        c.execute('UPDATE schema_meta SET version=4')
        names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_meta','sqlite_sequence')")]
        before={n:[tuple(r) for r in c.execute('SELECT * FROM '+n)] for n in names}
    with pytest.raises(StorageError):SQLiteStorage(store.path,existing_only=True)
    upgraded=SQLiteStorage(store.path)
    with upgraded._connect() as c:
        assert c.execute('SELECT version FROM schema_meta').fetchone()[0]==6
        assert {n:[tuple(r) for r in c.execute('SELECT * FROM '+n)] for n in names}==before
    assert len(upgraded.action_audit('joinquant','primary','reading')['history'])==1
    SQLiteStorage(store.path)
    assert len(upgraded.action_audit('joinquant','primary','reading')['history'])==1


def test_new_day_flow_after_review_still_inspects_and_never_replays_old_day(tmp_path):
    store,flame,args=setup(tmp_path)
    store.archive_action(**args)
    # Explicit local resume is a separate operation; archive alone never performs it.
    store.resume('joinquant','primary','reading')
    calls=[]
    class Gateway:
        def inspect(self,now):calls.append('inspect');return reading({'status':1})
        def __getattr__(self,name):raise AssertionError(name)
    result=JoinQuantBrowserKeeper()._run(SimpleNamespace(storage=store,account_id='primary',now=NOW,clock=FixedClock(NOW)),
        Gateway(),task_from_settings(flame.settings),{'reserved':False,'action_stage':'inspect'})
    assert result.status is KeepStatus.SUCCESS and calls==['inspect']
    # Today's observed success is not used to confirm the abandoned historical row.
    assert store.get_action('joinquant','primary','reading').state=='abandoned'
    assert store.action_audit('joinquant','primary','reading')['history'][0]['confirmed_at'] is None


def test_archive_cli_dry_run_does_not_open_database_evidence_or_browser(tmp_path,monkeypatch,capsys):
    config=write_config(tmp_path)
    monkeypatch.setattr('keepflame.cli.build_runtime',lambda *a,**kw:pytest.fail('runtime'))
    monkeypatch.setattr('keepflame.cli.SQLiteStorage',lambda *a,**kw:pytest.fail('database'))
    assert main(['archive-action','--config',str(config),'--flame','joinquant-primary','--day','2026-10-07',
        '--started-at',OLD.isoformat(),'--disposition','unknown','--request-id',str(uuid4()),'--evidence','/missing/not-read'])==0
    assert json.loads(capsys.readouterr().out)['database_opened'] is False


def test_archive_cli_execute_is_local_and_keeps_pause(tmp_path,monkeypatch,capsys):
    store,flame,args=setup(tmp_path)
    from tests.test_config import BASE_CONFIG
    path=write_config(tmp_path,BASE_CONFIG.replace('id = "joinquant-primary"','id = "reading"').replace(
        '[flames.settings]','[flames.settings]\ntask="reading"'))
    evidence=tmp_path/'evidence.json';evidence.write_text(json.dumps(REFS))
    monkeypatch.setattr('keepflame.cli.build_runtime',lambda *a,**kw:pytest.fail('runtime must not be created'))
    class Time:
        @staticmethod
        def now(tz):return NOW
        fromisoformat=staticmethod(datetime.fromisoformat)
    monkeypatch.setattr('keepflame.cli.datetime',Time)
    command=['archive-action','--execute','--config',str(path),'--flame','reading','--day','2026-10-07',
        '--started-at',OLD.isoformat(),'--disposition','abandoned','--request-id',args['request_id'],
        '--run-id',args['run_id'],'--evidence',str(evidence)]
    assert main(command)==0
    assert json.loads(capsys.readouterr().out)['pauses_changed'] is False
    assert main(command)==0
    capsys.readouterr()
    assert store.is_paused('joinquant','primary','reading')
    assert len(store.action_audit('joinquant','primary','reading')['archives'])==1
    assert main(['actions','--config',str(path),'--flame','reading'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['history'][0]['state']=='pending' and report['archives'][0]['disposition']=='abandoned'


def test_archive_and_next_day_concurrency_never_replays_old_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store,_,args=setup(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        decisions=list(pool.map(lambda _:SQLiteStorage(store.path).archive_action(**args),range(2)))
    assert decisions[0]==decisions[1]
    with ThreadPoolExecutor(2) as pool:
        writes=list(pool.map(lambda _:SQLiteStorage(store.path).reserve_action('joinquant','primary','reading','2026-10-09',NOW),range(2)))
    assert sorted(writes)==[False,True]
    assert len(store.action_audit('joinquant','primary','reading')['history'])==2
    assert not store.reserve_action('joinquant','primary','reading','2026-10-07',OLD)


def test_archive_stale_identity_after_new_reservation_cannot_clear_new_pending(tmp_path):
    store,_,args=setup(tmp_path)
    store.archive_action(**args)
    assert store.reserve_action('joinquant','primary','reading','2026-10-09',NOW)
    before=store.get_action('joinquant','primary','reading')
    with pytest.raises(ArchiveError):store.archive_action(**{**args,'request_id':str(uuid4())})
    assert store.get_action('joinquant','primary','reading')==before


def test_secret_or_duplicate_evidence_never_enters_audit(tmp_path):
    store,_,args=setup(tmp_path)
    for evidence in ({**REFS,'cookie':'SYNTHETIC_FORBIDDEN'}, {**REFS,'references':['not-a-digest']},
                     {**REFS,'references':['a'*64,'a'*64]}, {**REFS,'version':True}):
        with pytest.raises(ArchiveError):store.archive_action(**{**args,'evidence':evidence})
    assert store.action_audit('joinquant','primary','reading')['archives']==[]
