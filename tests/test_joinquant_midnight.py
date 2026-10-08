"""A live injected clock and production status parser fence all in-flight stages."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from types import SimpleNamespace
import time

import pytest

from keepflame.capabilities.http import HttpResponse
from keepflame.core.config import load_config
from keepflame.core.models import KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper
from keepflame.flames.joinquant.day import BusinessDayFence
from keepflame.flames.joinquant.guard import SignInGuard, FlowError, CHALLENGE_PATH
from keepflame.flames.joinquant.keeper import JoinQuantKeeper, TASK_INFO_URL, JOINQUANT_ORIGIN
from keepflame.storage import SQLiteStorage
from tests.helpers import FakeNotifier, StaticRegistry, StaticSecrets
from tests.test_config import write_config
from tests.test_joinquant_guard import Route, Reply

START = datetime(2026,10,8,15,58,50,tzinfo=timezone.utc)
ARTICLE = 'https://www.joinquant.com/view/community/detail/12345'


class Clock:
    def __init__(self):self.value=START
    def now(self):return self.value
    def cross(self):self.value+=timedelta(seconds=120)


def parsed(clock, complete=False, qualified=False):
    tasks={} if not (complete or qualified) else {'post_view':{'status':int(complete),'totalCount':1}}
    return JoinQuantKeeper._interpret(HttpResponse(200,TASK_INFO_URL,{'date':format_datetime(clock.now(),usegmt=True)},
        json.dumps({'code':'00000','data':{'taskList':tasks}}).encode()),now=clock.now(),task='reading')


@pytest.mark.parametrize('stage', ['initial_inspect','read_article','challenge','solver','submit','final_inspect'])
def test_crossing_at_each_stage_never_confirms_old_day_or_retries(tmp_path,stage):
    clock=Clock(); events=[]
    config=load_config(write_config(tmp_path));flame=replace(config.flames[0],id='reading',settings={
        'mode':'browser','task':'reading','article_url':ARTICLE,'reading_wait_seconds':120,'browser_profile_dir':'/synthetic/no-open'})
    storage=SQLiteStorage(config.storage.path)
    class Gateway:
        def __init__(self):self.checks=0
        def inspect(self,now):
            events.append('inspect');self.checks+=1
            if stage=='initial_inspect' and self.checks==1 or stage=='final_inspect' and self.checks==3:clock.cross()
            return parsed(clock,complete=self.checks==3,qualified=self.checks==2)
        def open(self):events.append('open')
        def read_article(self):
            events.append('read_article')
            if stage=='read_article':clock.cross()
        def challenge(self):
            events.append('challenge')
            if stage=='challenge':clock.cross()
            return {},'synthetic'
        def submit(self,x):
            events.append('submit')
            if stage=='submit':clock.cross()
        def close(self):events.append('close')
    @contextmanager
    def browser(**kw):yield object()
    gateway=Gateway()
    def solve(*args):
        events.append('solver')
        if stage=='solver':clock.cross()
        return SimpleNamespace(x=1,confidence=1.0)
    keeper=JoinQuantBrowserKeeper(page_factory=lambda *a,**kw:gateway,solver=solve,sleeper=lambda _:None)
    runner=Orchestrator(storage=storage,registry=StaticRegistry(keeper),
        capabilities=SimpleNamespace(require=lambda *args:SimpleNamespace(open=browser)),
        secrets=StaticSecrets(AssertionError('no credentials')),notifier=FakeNotifier(),clock=clock)
    outcome=runner.run_flame(flame,notify=False)
    action=storage.get_action('joinquant','primary','reading')
    assert outcome.record.result.status is not KeepStatus.SUCCESS
    assert outcome.record.result.metadata['reason']=='business_day_changed'
    if stage=='initial_inspect':assert action is None
    else:
        assert action.state=='pending' and action.day=='2026-10-08' and action.started_at==START
        assert action.confirmed_at is None
        assert storage.is_paused('joinquant','primary','reading')
    assert events.count('challenge')<=1 and events.count('submit')<=1
    if stage in {'initial_inspect','read_article','challenge','solver'}:assert 'submit' not in events
    if stage=='read_article':assert events==['inspect','read_article','close']
    (tmp_path/'midnight-evidence.json').write_text(json.dumps({'stage':stage,'events':events,
        'status':outcome.record.result.status.value,'reason':outcome.record.result.metadata['reason'],
        'original_day':None if action is None else action.day,'action_state':None if action is None else action.state,
        'finished_at':outcome.record.finished_at.isoformat()},indent=2)+'\n')


def test_midnight_during_confirmation_transaction_rolls_back_result_and_history(tmp_path):
    clock=Clock();store=SQLiteStorage(tmp_path/'state.sqlite3')
    assert store.reserve_action('joinquant','a','reading','2026-10-08',START)
    before=store.get_action('joinquant','a','reading')
    fence=BusinessDayFence(SimpleNamespace(now=START,clock=clock));calls=[]
    def validate():
        calls.append(1)
        if len(calls)==2:clock.cross()
        return fence.check()
    with pytest.raises(FlowError,match='business_day_changed'):
        store.confirm_action('joinquant','a','reading',START,day='2026-10-08',validate_time=validate)
    assert store.get_action('joinquant','a','reading')==before
    assert len(store.action_audit('joinquant','a','reading')['history'])==1


def test_midnight_during_reservation_does_not_leave_a_partial_intent(tmp_path):
    clock=Clock();store=SQLiteStorage(tmp_path/'state.sqlite3');fence=BusinessDayFence(SimpleNamespace(now=START,clock=clock));calls=[]
    def validate():
        calls.append(1)
        if len(calls)==2:clock.cross()
        return fence.check()
    with pytest.raises(FlowError):store.reserve_action('k','a','reading','2026-10-08',START,validate_time=validate)
    assert store.get_action('k','a','reading') is None
    assert store.action_audit('k','a','reading')['history']==[]


def test_guard_rejects_next_day_server_date_even_before_local_clock_crosses():
    clock=Clock();fence=BusinessDayFence(SimpleNamespace(now=START,clock=clock))
    page=SimpleNamespace(context=SimpleNamespace(route=lambda *args:None,unroute=lambda *args:None))
    response=lambda *a,**kw:(200,{'date':format_datetime(START+timedelta(seconds=120),usegmt=True)},b'{"code":"00000"}')
    guard=SignInGuard(page,deadline=time.monotonic()+60,task='reading',date_fence=fence,request_fetch=response)
    with pytest.raises(FlowError,match='business_day_changed'):guard.read_status()
    assert fence.changed


def test_guard_stops_write_at_midnight_before_native_transport():
    clock=Clock();fence=BusinessDayFence(SimpleNamespace(now=START,clock=clock));calls=[]
    page=SimpleNamespace(context=SimpleNamespace(route=lambda *a:None,unroute=lambda *a:None))
    guard=SignInGuard(page,deadline=time.monotonic()+60,date_fence=fence,request_fetch=lambda *a,**kw:calls.append('transport'))
    guard.arm_challenge();clock.cross()
    route=Route(Reply(),url=JOINQUANT_ORIGIN+CHALLENGE_PATH)
    guard.route(route)
    assert guard.failure.reason=='business_day_changed' and calls==[]
    assert guard.counts[CHALLENGE_PATH]==0
    clock.value=START
    with pytest.raises(FlowError,match='business_day_changed'):fence.check()
