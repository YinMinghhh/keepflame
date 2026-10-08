"""Date identity counterexamples: real SQLite, synthetic observations, no I/O."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from keepflame.core.models import KeepStatus
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper
from keepflame.flames.joinquant.task import task_from_settings
from keepflame.flames.joinquant.keeper import JoinQuantKeeper
from tests.helpers import response
import json
from keepflame.storage import SQLiteStorage
from tests.test_joinquant_reading import reading

NOW = datetime(2026, 10, 8, 15, 49, tzinfo=timezone.utc)


@pytest.mark.parametrize('task', ['reading', 'checkin'])
@pytest.mark.parametrize('offset', [-1, 1])
@pytest.mark.parametrize('state', ['complete', 'claimable', 'needs_reading'])
def test_other_day_pending_is_unchanged_after_current_observation(tmp_path, task, offset, state):
    path = tmp_path/'state.sqlite3'
    store = SQLiteStorage(path)
    original_time = NOW + timedelta(days=offset)
    day = original_time.date().isoformat()
    assert store.reserve_action('joinquant', 'synthetic', task, day, original_time)
    original = store.get_action('joinquant', 'synthetic', task)
    store = SQLiteStorage(path)  # A restart must not weaken the boundary.
    observed = {'complete': reading({'status': 1}),
                'claimable': reading({'status': 0, 'totalCount': 1}),
                'needs_reading': reading()}[state]
    if task == 'checkin':
        observed = JoinQuantKeeper._interpret(response(200, json.dumps({'code':'00000','data':{'taskList':{'login':{'status':int(state == 'complete')}}}})),now=NOW)
    events = []
    class Gateway:
        def open(self): events.append('open')
        def inspect(self, now):
            events.append('inspect')
            return observed
        def __getattr__(self, name): raise AssertionError('unexpected action '+name)
    settings = {'task':task, 'article_url':'https://www.joinquant.com/view/community/detail/12345'}
    context = SimpleNamespace(storage=store, account_id='synthetic', now=NOW)
    result = JoinQuantBrowserKeeper()._run(context, Gateway(), task_from_settings(settings),
                                          {'reserved':False, 'action_stage':'inspect'})
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.metadata['unconfirmed_reason'] == 'pending_different_day'
    if task == 'reading':
        assert result.metadata['reading_state'] == state
    else:
        assert result.metadata['task_status'] == int(state == 'complete')
    assert result.metadata['automation_paused'] is True
    assert store.get_action('joinquant', 'synthetic', task) == original
    assert events == (['open', 'inspect'] if task == 'checkin' else ['inspect'])
    assert not store.reserve_action('joinquant', 'synthetic', task, '2026-10-08', NOW)


@pytest.mark.parametrize('day', ['2026-10-07', '2026-10-09'])
def test_storage_rejects_mismatching_day_without_mutation(tmp_path, day):
    store = SQLiteStorage(tmp_path/'state.sqlite3')
    assert store.reserve_action('k','a','reading','2026-10-08',NOW)
    before = store.get_action('k','a','reading')
    assert store.confirm_action('k','a','reading',NOW,day=day) is False
    assert store.get_action('k','a','reading') == before
    # Date identity is explicit, including for callers outside the Keeper.
    with pytest.raises(TypeError): store.confirm_action('k','a','reading',NOW)
    assert store.get_action('k','a','reading') == before


def test_same_day_confirmation_and_duplicate_are_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path/'state.sqlite3'
    store = SQLiteStorage(path)
    assert store.reserve_action('k','a','reading','2026-10-08',NOW)
    def confirm(_):
        return SQLiteStorage(path).confirm_action('k','a','reading',NOW,day='2026-10-08')
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(confirm, range(2))) == [False, True]
    after = store.get_action('k','a','reading')
    assert after.state == 'confirmed' and after.day == '2026-10-08' and after.started_at == NOW
    assert not store.reserve_action('k','a','reading','2026-10-08',NOW)


def test_same_day_observation_can_confirm_pending_without_another_action(tmp_path):
    store = SQLiteStorage(tmp_path/'state.sqlite3')
    assert store.reserve_action('joinquant','synthetic','reading','2026-10-08',NOW)
    gateway = SimpleNamespace(inspect=lambda now:reading({'status':1}))
    context = SimpleNamespace(storage=store,account_id='synthetic',now=NOW)
    task = task_from_settings({'task':'reading','article_url':'https://www.joinquant.com/view/community/detail/12345'})
    result = JoinQuantBrowserKeeper()._run(context,gateway,task,{'reserved':False,'action_stage':'inspect'})
    assert result.status is KeepStatus.SUCCESS and result.metadata['automatic_action'] is False
    assert store.get_action('joinquant','synthetic','reading').state == 'confirmed'


def test_lost_confirmation_identity_does_not_report_success():
    pending = SimpleNamespace(state='pending',day='2026-10-08')
    store = SimpleNamespace(get_action=lambda *args:pending, confirm_action=lambda *args,**kwargs:False)
    gateway = SimpleNamespace(inspect=lambda now:reading({'status':1}))
    context = SimpleNamespace(storage=store,account_id='synthetic',now=NOW)
    task = task_from_settings({'task':'reading','article_url':'https://www.joinquant.com/view/community/detail/12345'})
    result = JoinQuantBrowserKeeper()._run(context,gateway,task,{'reserved':False,'action_stage':'inspect'})
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.metadata['unconfirmed_reason'] == 'action_confirmation_rejected'
