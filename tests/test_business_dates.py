"""Business targets survive long operations, cleanup, restart and queue delay.

Every clock, response and browser below is synthetic. No real profile or network.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import io
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.cli import _outcome_dict, _state_dict
from keepflame.core.config import AppConfig, FlameConfig, ScheduleConfig, StorageConfig
from keepflame.core.models import BusinessTarget, CompletionReceipt, KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.protocols import AutomationPaused
from keepflame.core.schedule import due_at
from keepflame.core.service import SchedulerService
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper
from keepflame.notifier import JsonlNotifier
from keepflame.storage import SQLiteStorage
from keepflame.storage.sqlite import StorageError
from tests.helpers import FixedClock, FakeNotifier, StaticRegistry, StaticSecrets
from tests.test_joinquant_midnight import parsed

UTC = timezone.utc


def local(zone='Asia/Shanghai', day=8, hour=23, minute=58, second=50):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=ZoneInfo(zone)).astimezone(UTC)


def setup(tmp_path, zone='Asia/Shanghai', start=None, keeper=None):
    clock = FixedClock(start or local(zone))
    flame = FlameConfig('reading', 'joinquant' if zone == 'Asia/Shanghai' else 'synthetic', 'primary', True,
        ScheduleConfig(zone, time(0), 1, time(20), time(23, 59, 59)),
        {'mode':'browser', 'task':'reading', 'article_url':'https://www.joinquant.com/view/community/detail/12345',
         'reading_wait_seconds':69, 'browser_profile_dir':'/synthetic/never-opened'})
    store = SQLiteStorage(tmp_path/'state.sqlite3')
    @contextmanager
    def browser(**kw):
        assert kw['profile_dir'] == '/synthetic/never-opened'
        yield object()
    runner = Orchestrator(storage=store, registry=StaticRegistry(keeper), clock=clock,
        capabilities=CapabilityRegistry({'browser':SimpleNamespace(open=browser)}),
        secrets=StaticSecrets(AssertionError('no credentials')), notifier=FakeNotifier())
    config = AppConfig(StorageConfig(store.path), (flame,), tmp_path/'synthetic.toml')
    service = SchedulerService(config=config, storage=store, orchestrator=runner, clock=clock)
    return flame, store, clock, runner, service


def browser_keeper(clock, *, step_seconds=69, cleanup_seconds=2, cleanup_error=False):
    events=[]
    class Gateway:
        checks=0
        def inspect(self, now):
            self.checks += 1; events.append('inspect')
            return parsed(clock, complete=self.checks==3, qualified=self.checks==2)
        def read_article(self):
            events.append('read_article'); clock.current += timedelta(seconds=step_seconds)
        def open(self): events.append('open')
        def challenge(self): events.append('challenge'); return {},'synthetic'
        def submit(self, x): events.append('submit')
        def close(self):
            events.append('close'); clock.current += timedelta(seconds=cleanup_seconds)
            if cleanup_error: raise RuntimeError('synthetic close failure')
    keeper = JoinQuantBrowserKeeper(page_factory=lambda *a,**kw:Gateway(),
        solver=lambda *a:SimpleNamespace(x=1, confidence=1.0), sleeper=lambda _:None)
    return keeper, events


@pytest.mark.parametrize('entry', ['direct', 'scheduler'])
@pytest.mark.parametrize('cleanup', [0, 2, 3600, 172802])
def test_confirmed_prior_day_success_survives_cleanup_and_restart(tmp_path, entry, cleanup):
    flame, store, clock, runner, service = setup(tmp_path)
    keeper, events = browser_keeper(clock, cleanup_seconds=cleanup)
    runner.registry = StaticRegistry(keeper)
    outcome = runner.run_flame(flame, notify=False) if entry=='direct' else service.run_due()[0][0]
    result = outcome.record.result
    action = store.get_action('joinquant','primary','reading')
    confirmed = local(second=59, minute=59)
    assert result.status is KeepStatus.SUCCESS
    assert action.day == result.receipt.target.day == outcome.record.business_target.day == '2026-10-08'
    assert action.confirmed_at == result.receipt.observed_at == outcome.state.last_success_at == confirmed
    assert outcome.record.finished_at == confirmed + timedelta(seconds=cleanup) == clock.current
    assert outcome.state.last_checked_at == clock.current
    assert not store.is_paused('joinquant','primary','reading')
    assert events.count('challenge') == events.count('submit') == events.count('read_article') == 1
    reopened = SQLiteStorage(store.path, existing_only=True)
    state = reopened.get_state(flame.id,flame.account_id)
    assert state == outcome.state and state.last_success_receipt == result.receipt
    # When cleanup spans several days, no retrospective reward catch-up occurs.
    tomorrow = local(day=9, hour=9, minute=0, second=0)
    now = max(tomorrow, clock.current)
    assert due_at(flame.schedule,state,now) == now
    with reopened._connect() as db:
        row = db.execute('SELECT * FROM keep_runs').fetchone()
    assert json.loads(row['completion_receipt_json']) == result.receipt.to_dict()
    assert json.loads(row['business_target_json']) == result.receipt.target.to_dict()
    assert datetime.fromisoformat(row['finished_at']) == clock.current
    assert _outcome_dict(outcome)['completion_receipt'] == result.receipt.to_dict()
    assert _state_dict(flame,state)['last_success_receipt'] == result.receipt.to_dict()


@pytest.mark.parametrize('step', [0, 30, 69, 70, 120, 86400])
def test_long_steps_split_valid_completion_from_unknown_actions(tmp_path, step):
    flame, store, clock, runner, _ = setup(tmp_path)
    keeper, events = browser_keeper(clock, step_seconds=step, cleanup_seconds=2)
    runner.registry=StaticRegistry(keeper)
    outcome=runner.run_flame(flame,notify=False)
    action=store.get_action('joinquant','primary','reading')
    if step < 70:
        assert outcome.record.result.status is KeepStatus.SUCCESS and action.state=='confirmed'
        assert not store.is_paused('joinquant','primary','reading')
    else:
        assert outcome.record.result.status is KeepStatus.FAILED
        assert outcome.record.result.receipt is None and action.state=='pending'
        assert outcome.record.result.metadata['reason']=='business_day_changed'
        assert 'challenge' not in events and 'submit' not in events
        with pytest.raises(AutomationPaused): runner.run_flame(flame,notify=False)
        assert store.get_action('joinquant','primary','reading')==action


@pytest.mark.parametrize('zone', ['Asia/Shanghai','UTC','America/Los_Angeles','Europe/Berlin'])
@pytest.mark.parametrize('cleanup', [0,2,86402])
def test_core_uses_target_timezone_and_completion_receipt_not_cleanup_date(tmp_path,zone,cleanup):
    start=local(zone, minute=59,second=58)
    flame,store,clock,runner,service=setup(tmp_path,zone,start=start)
    class Keeper:
        def keep(self, context):
            clock.current += timedelta(seconds=1)
            receipt=context.completion_receipt()
            clock.current += timedelta(seconds=cleanup)
            return KeepResult(KeepStatus.SUCCESS,'completed',receipt=receipt)
    runner.registry=StaticRegistry(Keeper())
    outcome=service.run_due()[0][0]
    assert outcome.state.last_success_receipt.target==BusinessTarget('2026-10-08',zone)
    assert outcome.state.last_success_at==start+timedelta(seconds=1)
    assert outcome.record.finished_at==clock.current
    assert due_at(flame.schedule,outcome.state,max(clock.current,local(zone,day=9,hour=9,minute=0,second=0)))==max(clock.current,local(zone,day=9,hour=9,minute=0,second=0))
    assert not store.is_paused(flame.keeper,'primary','reading')


def test_actual_cleanup_error_preserves_success_but_pauses_resource_reuse(tmp_path):
    flame,store,clock,runner,_=setup(tmp_path)
    keeper,_=browser_keeper(clock,cleanup_error=True)
    runner.registry=StaticRegistry(keeper)
    outcome=runner.run_flame(flame,notify=False)
    assert outcome.record.result.status is KeepStatus.SUCCESS
    assert outcome.state.last_success_at==local(minute=59,second=59)
    assert outcome.record.result.metadata['reason']=='browser_cleanup_failed'
    assert store.get_action('joinquant','primary','reading').state=='confirmed'
    assert store.is_paused('joinquant','primary','reading')


@pytest.mark.parametrize('receipt_mode',['none','forged','previous_run'])
def test_crossday_success_requires_receipt_issued_for_this_run(tmp_path,receipt_mode):
    flame,store,clock,runner,_=setup(tmp_path)
    class Keeper:
        old=None
        def keep(self,context):
            target=context.business_target
            receipt=None
            if receipt_mode=='forged': receipt=CompletionReceipt(target,clock.current)
            if receipt_mode=='previous_run':
                if self.old is None: self.old=context.completion_receipt(); return KeepResult(KeepStatus.SUCCESS,'first',receipt=self.old)
                receipt=self.old
            clock.current+=timedelta(seconds=80)
            return KeepResult(KeepStatus.SUCCESS,'unverified',receipt=receipt)
    keeper=Keeper();runner.registry=StaticRegistry(keeper)
    if receipt_mode=='previous_run':
        assert runner.run_flame(flame,notify=False).record.result.status is KeepStatus.SUCCESS
        clock.current+=timedelta(days=1)
    outcome=runner.run_flame(flame,notify=False)
    assert outcome.record.result.status is KeepStatus.FAILED and outcome.record.result.receipt is None
    assert outcome.record.result.metadata['reason']=='completion_receipt_missing_or_invalid'
    assert store.is_paused(flame.keeper,'primary','reading')


def test_clock_rollback_after_completion_preserves_receipt_and_actual_wall_times(tmp_path):
    flame,store,clock,runner,_=setup(tmp_path)
    start=clock.current
    class Keeper:
        def keep(self,context):
            receipt=context.completion_receipt();clock.current-=timedelta(minutes=1)
            return KeepResult(KeepStatus.SUCCESS,'completed',receipt=receipt)
    runner.registry=StaticRegistry(Keeper())
    outcome=runner.run_flame(flame,notify=False)
    assert outcome.record.clock_regressed and outcome.record.started_at==start
    assert outcome.record.finished_at==clock.current < start
    assert outcome.state.last_success_at==start and outcome.record.result.status is KeepStatus.SUCCESS
    assert store.is_paused(flame.keeper,'primary','reading')
    assert due_at(flame.schedule,outcome.state,clock.current)>=local(day=9,hour=0,minute=0,second=0)


def test_same_day_legacy_keeper_receipt_is_at_return_not_admission(tmp_path):
    flame,store,clock,runner,_=setup(tmp_path)
    class Keeper:
        def keep(self,context):
            clock.current+=timedelta(seconds=10)
            return KeepResult(KeepStatus.SUCCESS,'completed')
    runner.registry=StaticRegistry(Keeper())
    outcome=runner.run_flame(flame,now=clock.current,notify=False)
    assert outcome.record.result.receipt.source=='core_return'
    assert outcome.state.last_success_at==clock.current
    assert outcome.record.finished_at>outcome.record.started_at


def test_failure_counter_and_notification_use_admitted_day_even_after_cleanup(tmp_path):
    flame,store,clock,runner,_=setup(tmp_path)
    class Keeper:
        def keep(self,context):
            clock.current+=timedelta(seconds=80)
            return KeepResult(KeepStatus.FAILED,'offline')
    runner.registry=StaticRegistry(Keeper())
    first=runner.run_flame(flame,notify=False)
    # An unpaused first failure retries silently; the attempt is counted on the admitted day.
    assert store.get_notification(flame.id,flame.account_id) is None
    assert first.state.last_checked_target.day=='2026-10-08' and first.state.last_checked_at==clock.current
    assert first.record.result.metadata['retry_attempt']==1
    assert first.record.result.metadata['business_target']['day']=='2026-10-08'
    second=runner.run_flame(flame,notify=False)
    assert second.state.consecutive_failures==1 and second.state.last_checked_target.day=='2026-10-09'
    assert second.record.result.metadata['retry_attempt']==1


def test_pending_receipt_notification_round_trips_and_jsonl_exposes_target(tmp_path):
    flame,store,clock,runner,_=setup(tmp_path,start=local(hour=9,minute=0,second=0))
    runner.registry=StaticRegistry(SimpleNamespace(keep=lambda _:KeepResult(KeepStatus.FAILED,'offline')))
    runner.run_flame(flame,notify=False)
    clock.current+=timedelta(hours=1)
    runner.registry=StaticRegistry(SimpleNamespace(keep=lambda _:KeepResult(KeepStatus.SUCCESS,'complete')))
    outcome=runner.run_flame(flame,notify=False)
    pending=SQLiteStorage(store.path).get_notification(flame.id,flame.account_id)
    assert pending.result.receipt==outcome.record.result.receipt
    assert pending.business_target==outcome.record.business_target
    stream=io.StringIO();runner.notifier=JsonlNotifier(stream);runner.deliver_pending(flame,now=clock.current)
    assert json.loads(stream.getvalue())['completion_receipt']==outcome.record.result.receipt.to_dict()


@pytest.mark.parametrize('legacy_version',[1,2,3,4,5])
def test_migration_never_backdates_historical_success_or_rewrites_old_rows(tmp_path,legacy_version):
    flame,store,clock,runner,_=setup(tmp_path)
    runner.registry=StaticRegistry(SimpleNamespace(keep=lambda _:KeepResult(KeepStatus.SUCCESS,'old completion')))
    runner.run_flame(flame,notify=False)
    with store._connect() as db:
        for table,columns in {'keep_runs':['business_target_json','completion_receipt_json','clock_regressed'],
                'flame_state':['last_checked_target_json','last_success_receipt_json'],
                'run_requests':['target_day','target_timezone']}.items():
            for column in columns: db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        db.execute('UPDATE schema_meta SET version=?',(legacy_version,))
        names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_meta','sqlite_sequence')")]
        columns={name:[r[1] for r in db.execute(f'PRAGMA table_info({name})')] for name in names}
        before={name:[tuple(r) for r in db.execute(f'SELECT * FROM {name}')] for name in names}
    with pytest.raises(StorageError): SQLiteStorage(store.path,existing_only=True)
    upgraded=SQLiteStorage(store.path)
    with upgraded._connect() as db:
        assert db.execute('SELECT version FROM schema_meta').fetchone()[0]==6
        after={name:[tuple(r) for r in db.execute(f'SELECT {",".join(columns[name])} FROM {name}')] for name in names}
        assert after==before
        assert db.execute('SELECT completion_receipt_json,business_target_json FROM keep_runs').fetchone()[:] == (None,None)
    state=upgraded.get_state(flame.id,flame.account_id)
    assert state.last_success_at==clock.current
    assert state.last_success_receipt is None and state.last_checked_target is None


@pytest.mark.parametrize('change',['midnight','legacy_missing_target','timezone','same_day'])
def test_daemon_queue_binds_business_target_and_never_reinterprets_stale_requests(tmp_path,monkeypatch,change):
    from tests.test_daemon_dispatch import setup as dispatch_setup, request
    runtime,browser,pool,visits,clock,stream=dispatch_setup(tmp_path,monkeypatch)
    flame=runtime.config.flames[0]
    clock.current=local(hour=10,minute=0,second=0) if change!='midnight' else local(hour=23,minute=59,second=30)
    with runtime.daemon_session():
        uid=str(uuid4());row=runtime.storage.submit_run(uid,runtime.config,flame,clock.current)
        assert (row['target_day'],row['target_timezone'])==('2026-10-08','Asia/Shanghai')
        if change=='midnight':clock.current+=timedelta(seconds=40)
        if change=='same_day':clock.current+=timedelta(seconds=20)
        if change in {'legacy_missing_target','timezone'}:
            with runtime.storage._connect() as db:
                db.execute('UPDATE run_requests SET target_timezone=? WHERE id=?',
                           (None if change=='legacy_missing_target' else 'UTC',uid))
        runtime.dispatch_one()
        row=request(runtime,uid)
        if change=='same_day':
            assert row['state']=='completed' and row['result_status']=='SUCCESS' and len(visits)==1
            with runtime.storage._connect() as db:
                run=db.execute('SELECT business_target_json FROM keep_runs WHERE run_id=?',(row['run_id'],)).fetchone()
            assert json.loads(run[0])=={'day':'2026-10-08','timezone':'Asia/Shanghai'}
        else:
            assert row['state']=='rejected' and row['reason'] in {'business_day_changed','business_timezone_changed'}
            assert not visits and runtime.storage.count_runs()==0
        assert runtime.storage.submit_run(uid,runtime.config,flame,clock.current)==row
        assert request(runtime,uid)['target_day']=='2026-10-08'


def test_queued_target_is_rechecked_at_keeper_entry_if_dispatch_itself_crosses_midnight(tmp_path,monkeypatch):
    from tests.test_daemon_dispatch import setup as dispatch_setup, request
    runtime,browser,pool,visits,clock,stream=dispatch_setup(tmp_path,monkeypatch)
    flame=replace(runtime.config.flames[0],schedule=replace(runtime.config.flames[0].schedule,stop_at=time(23,59,59)))
    runtime.config=replace(runtime.config,flames=(flame,))
    clock.current=local(hour=23,minute=59,second=58)
    emit=runtime.notifier.emit
    def crossing_emit(event,**fields):
        emit(event,**fields)
        if event=='daemon_run_started':clock.current+=timedelta(seconds=3)
    monkeypatch.setattr(runtime.notifier,'emit',crossing_emit)
    with runtime.daemon_session():
        uid=str(uuid4());runtime.storage.submit_run(uid,runtime.config,flame,clock.current)
        runtime.dispatch_one()
        assert not visits
        row=request(runtime,uid)
        assert row['result_status']=='FAILED'
        state=runtime.storage.get_state(flame.id,flame.account_id)
        assert state.last_checked_target.day=='2026-10-08' and state.last_success_at is None


def test_joinquant_timezone_mismatch_is_rejected_before_browser_or_intent(tmp_path):
    flame,store,clock,runner,_=setup(tmp_path,zone='UTC')
    keeper,events=browser_keeper(clock)
    runner.registry=StaticRegistry(keeper)
    outcome=runner.run_flame(flame,notify=False)
    assert outcome.record.result.status is KeepStatus.FAILED and not events
    assert store.get_action('joinquant','primary','reading') is None


def test_dst_transition_keeps_business_day_and_schedules_next_local_day(tmp_path):
    zone='America/New_York'
    start=datetime(2026,11,1,1,59,50,tzinfo=ZoneInfo(zone),fold=0).astimezone(UTC)
    flame,store,clock,runner,_=setup(tmp_path,zone=zone,start=start)
    class Keeper:
        def keep(self,context):
            clock.current+=timedelta(seconds=20)  # Fall-back repeats 01:00, still the same date.
            receipt=context.completion_receipt()
            return KeepResult(KeepStatus.SUCCESS,'complete',receipt=receipt)
    runner.registry=StaticRegistry(Keeper())
    outcome=runner.run_flame(flame,notify=False)
    assert outcome.state.last_success_receipt.target.day=='2026-11-01'
    assert outcome.record.result.next_action_at==datetime(2026,11,2,tzinfo=ZoneInfo(zone)).astimezone(UTC)


def test_confirmation_committed_before_midnight_is_not_undone_by_return_delay(tmp_path,monkeypatch):
    flame,store,clock,runner,_=setup(tmp_path)
    keeper,_=browser_keeper(clock,cleanup_seconds=0)
    runner.registry=StaticRegistry(keeper)
    confirm=store.confirm_action
    def confirmed_then_delayed(*args,**kwargs):
        result=confirm(*args,**kwargs)
        assert result
        clock.current+=timedelta(seconds=2)
        return result
    monkeypatch.setattr(store,'confirm_action',confirmed_then_delayed)
    outcome=runner.run_flame(flame,notify=False)
    assert outcome.record.result.status is KeepStatus.SUCCESS
    assert outcome.state.last_success_at==local(minute=59,second=59)
    assert outcome.record.finished_at==local(day=9,hour=0,minute=0,second=1)
    assert not store.is_paused('joinquant','primary','reading')


def test_persistence_delay_does_not_reassign_success_or_notification_day(tmp_path,monkeypatch):
    flame,store,clock,runner,_=setup(tmp_path)
    keeper,_=browser_keeper(clock,cleanup_seconds=0)
    runner.registry=StaticRegistry(keeper)
    record=store.record
    def slow_record(*args,**kwargs):
        clock.current+=timedelta(seconds=2)
        return record(*args,**kwargs)
    monkeypatch.setattr(store,'record',slow_record)
    outcome=runner.run_flame(flame,notify=False)
    # Run finish denotes the completed Keeper/cleanup; storage delay cannot
    # relabel its receipt. The scheduler independently samples the live clock.
    assert outcome.record.finished_at==outcome.state.last_success_at==local(minute=59,second=59)
    assert clock.current==local(day=9,hour=0,minute=0,second=1)
    state=SQLiteStorage(store.path).get_state(flame.id,flame.account_id)
    assert state.last_success_receipt.target.day=='2026-10-08'
    assert due_at(flame.schedule,state,clock.current)==clock.current


def test_scheduler_samples_time_again_between_flames_and_after_cleanup(tmp_path):
    flame,store,clock,runner,service=setup(tmp_path)
    seen=[]
    class Keeper:
        def keep(self,context):
            seen.append(context.business_target.day)
            receipt=context.completion_receipt()
            clock.current+=timedelta(seconds=80)
            return KeepResult(KeepStatus.SUCCESS,'complete',receipt=receipt)
    runner.registry=StaticRegistry(Keeper())
    other=replace(flame,id='other',account_id='other')
    service.config=replace(service.config,flames=(flame,other))
    outcomes,_=service.run_due(now=clock.current)
    assert seen==['2026-10-08','2026-10-09'] and len(outcomes)==2
    assert outcomes[0].record.finished_at==outcomes[1].record.started_at
    assert outcomes[1].record.finished_at==clock.current


def test_late_retry_after_is_measured_from_observation_not_admission(tmp_path):
    flame,store,clock,runner,_=setup(tmp_path)
    from keepflame.core.protocols import KeeperContext
    from keepflame.flames.joinquant.guard import FlowError
    class Gateway:
        def inspect(self,now):
            clock.current+=timedelta(seconds=50)
            raise FlowError('status_http',429,'172800')
        def close(self): pass
    keeper=JoinQuantBrowserKeeper(page_factory=lambda *a,**kw:Gateway())
    runner.registry=StaticRegistry(keeper)
    outcome=runner.run_flame(flame,notify=False)
    assert outcome.record.result.next_action_at>=clock.current+timedelta(days=2)
    assert store.is_paused('joinquant','primary','reading')
