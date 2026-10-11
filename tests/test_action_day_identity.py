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


from collections import Counter
from datetime import time
from uuid import uuid4

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.config import AppConfig, FlameConfig, ScheduleConfig, StorageConfig
from keepflame.core.eligibility import execution_paused
from keepflame.core.models import BusinessTarget, FlameState, KeepResult, RunRecord
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.protocols import KeeperContext
from keepflame.core.service import SchedulerService
from keepflame.flames.joinquant.browser import FlowError
from tests.helpers import FakeNotifier, FixedClock, SequenceKeeper, StaticRegistry, StaticSecrets
from tests.test_joinquant_auto import FakeBrowser, FakePage, unsigned


STARTED = datetime(2026, 10, 10, 14, 40, 24, 872681, tzinfo=timezone.utc)
OCTOBER_11 = datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai


def _archives(store):
    with store._connect() as connection:
        return connection.execute('SELECT COUNT(*) FROM action_archives').fetchone()[0]


def _seed_checkin(store, claim, *, challenge=None, answer=None, flame_id='joinquant-primary', record_run=True):
    assert store.reserve_action('joinquant', 'primary', 'checkin', '2026-10-10', STARTED)
    if not record_run:
        store.pause('joinquant', 'primary', flame_id, 'signin_button_changed', STARTED)
        return
    counts = {'claim': claim}
    if challenge is not None:
        counts['challenge'] = challenge
    if answer is not None:
        counts['answer'] = answer
    result = KeepResult(
        KeepStatus.FAILED,
        'JoinQuant automatic task stopped: signin_button_changed',
        metadata={
            'reason': 'signin_button_changed',
            'execution_mode': 'browser',
            'stage_counts': counts,
        },
    )
    target = BusinessTarget('2026-10-10', 'Asia/Shanghai')
    store.record(RunRecord(
        run_id=str(uuid4()), flame_id=flame_id, account_id='primary',
        started_at=STARTED, finished_at=STARTED, result=result, business_target=target,
    ), FlameState(
        flame_id=flame_id, account_id='primary', status=KeepStatus.FAILED, message=result.message,
        last_checked_at=STARTED, last_success_at=None, consecutive_failures=1,
        pending_human_since=None, human_action=None, next_action_at=None, last_reminder_at=None,
        metadata=result.metadata, last_checked_target=target,
    ))
    store.pause('joinquant', 'primary', flame_id, 'signin_button_changed', STARTED)


def _shanghai_flame(tmp_path):
    return FlameConfig(
        id='joinquant-primary', keeper='joinquant', account_id='primary', enabled=True,
        schedule=ScheduleConfig(
            timezone='Asia/Shanghai', first_check=time(9), recheck_minutes=60,
            reminder_at=time(20), stop_at=time(23),
        ),
        settings={'mode': 'browser', 'task': 'checkin'},
    ), AppConfig(
        storage=StorageConfig(tmp_path / 'unused.sqlite3'),
        flames=(),
        source_path=tmp_path / 'keepflame.toml',
    )


def test_zero_claim_2026_10_10_can_reserve_on_2026_10_11(tmp_path):
    store = SQLiteStorage(tmp_path / 'zero-claim.sqlite3')
    _seed_checkin(store, 0)
    flame, config = _shanghai_flame(tmp_path)
    config = AppConfig(config.storage, (flame,), config.source_path)
    clock = FixedClock(OCTOBER_11)
    service = SchedulerService(
        config=config, storage=store, clock=clock,
        orchestrator=Orchestrator(
            storage=store, registry=StaticRegistry(SequenceKeeper(KeepResult(KeepStatus.SUCCESS, 'healthy'))),
            capabilities=CapabilityRegistry({}), secrets=StaticSecrets('unused'),
            notifier=FakeNotifier(), clock=clock,
        ),
    )
    outcomes, _next = service.run_due(now=OCTOBER_11)
    assert len(outcomes) == 1
    assert execution_paused(store, 'joinquant', 'primary', 'joinquant-primary', now=OCTOBER_11) is False
    assert store.is_paused('joinquant', 'primary', 'joinquant-primary') is False
    action = store.get_action('joinquant', 'primary', 'checkin')
    assert action.state == 'pending' and action.day == '2026-10-10'
    assert _archives(store) == 0
    assert store.reserve_action('joinquant', 'primary', 'checkin', '2026-10-11', OCTOBER_11) is True
    reserved = store.get_action('joinquant', 'primary', 'checkin')
    assert reserved.state == 'pending' and reserved.day == '2026-10-11'
    assert _archives(store) == 0
    print('observation zero_claim_2026-10-10_reserves_2026-10-11_without_archive=true')


@pytest.mark.parametrize("challenge,answer", [(1, 0), (0, 1)])
def test_submitted_challenge_or_answer_stays_stopped_on_the_next_day(tmp_path, challenge, answer):
    store = SQLiteStorage(tmp_path / f"submitted-{challenge}-{answer}.sqlite3")
    _seed_checkin(store, 0, challenge=challenge, answer=answer)
    assert store.blocks_execution("joinquant", "primary", "joinquant-primary", now=OCTOBER_11) is True
    assert execution_paused(store, "joinquant", "primary", "joinquant-primary", now=OCTOBER_11) is True
    assert store.is_paused("joinquant", "primary", "joinquant-primary") is True
    assert store.zero_claim_pending_released("joinquant", "primary", "checkin", "2026-10-11") is False
    assert store.reserve_action("joinquant", "primary", "checkin", "2026-10-11", OCTOBER_11) is False
    action = store.get_action("joinquant", "primary", "checkin")
    assert action.state == "pending" and action.day == "2026-10-10"
    assert _archives(store) == 0
    print(f"observation submitted_write_stays_stopped_next_day challenge={challenge} answer={answer} claim=0")


def test_submitted_claim_stays_paused_and_cannot_reserve(tmp_path):
    store = SQLiteStorage(tmp_path / 'submitted.sqlite3')
    _seed_checkin(store, 1)
    assert execution_paused(store, 'joinquant', 'primary', 'joinquant-primary', now=OCTOBER_11) is True
    assert store.is_paused('joinquant', 'primary', 'joinquant-primary') is True
    assert store.reserve_action('joinquant', 'primary', 'checkin', '2026-10-11', OCTOBER_11) is False
    action = store.get_action('joinquant', 'primary', 'checkin')
    assert action.state == 'pending' and action.day == '2026-10-10'
    assert _archives(store) == 0
    print('observation submitted_unconfirmed_claim_cannot_reserve=true')


def test_same_day_and_missing_count_and_account_pause_stay_blocked(tmp_path):
    same_day = SQLiteStorage(tmp_path / 'same-day.sqlite3')
    _seed_checkin(same_day, 0)
    assert execution_paused(same_day, 'joinquant', 'primary', 'joinquant-primary', now=STARTED) is True
    assert same_day.reserve_action('joinquant', 'primary', 'checkin', '2026-10-10', STARTED) is False
    print('observation same_day_zero_claim_stays_blocked=true')

    missing = SQLiteStorage(tmp_path / 'missing.sqlite3')
    _seed_checkin(missing, 0, record_run=False)
    assert execution_paused(missing, 'joinquant', 'primary', 'joinquant-primary', now=OCTOBER_11) is True
    assert missing.reserve_action('joinquant', 'primary', 'checkin', '2026-10-11', OCTOBER_11) is False
    assert missing.get_action('joinquant', 'primary', 'checkin').day == '2026-10-10'
    print('observation missing_claim_count_stays_blocked=true')

    account = SQLiteStorage(tmp_path / 'account.sqlite3')
    _seed_checkin(account, 0)
    account.pause('joinquant', 'primary', '*', 'login_required', STARTED)
    assert execution_paused(account, 'joinquant', 'primary', 'joinquant-primary', now=OCTOBER_11) is True
    assert account.is_paused('joinquant', 'primary', '*') is True
    assert account.is_paused('joinquant', 'primary', 'joinquant-primary') is True
    print('observation account_pause_stays=true')


def test_zero_claim_keeper_does_not_stop_on_pending_different_day(tmp_path):
    store = SQLiteStorage(tmp_path / 'keeper.sqlite3')
    _seed_checkin(store, 0)
    page = FakePage([unsigned(), unsigned()], challenge_error=FlowError('signin_button_changed'))
    page.guard = SimpleNamespace(counts=Counter({'/credits/task/receiveCredits': 0}), failure=None)
    context = KeeperContext(
        flame_id='joinquant-primary', account_id='primary',
        settings={'mode': 'browser', 'browser_profile_dir': '/synthetic-profile', 'browser_headless': True},
        capabilities=CapabilityRegistry({'browser': FakeBrowser()}),
        secrets=StaticSecrets(AssertionError('the browser flow must not read secrets')),
        storage=store, now=OCTOBER_11, clock=FixedClock(OCTOBER_11),
    )
    result = JoinQuantBrowserKeeper(
        page_factory=lambda *_args, **_kwargs: page, sleeper=lambda _seconds: None,
    ).keep(context)
    assert result.metadata.get('unconfirmed_reason') != 'pending_different_day'
    assert 'pending_different_day' not in json.dumps(result.metadata)
    assert result.metadata['reason'] == 'signin_button_changed'
    assert 'automation_paused' not in result.metadata
    assert page.challenges == 1 and page.answers == []
    action = store.get_action('joinquant', 'primary', 'checkin')
    assert action.state == 'pending' and action.day == '2026-10-11'
    assert _archives(store) == 0
    print('observation zero_claim_keeper_retries_next_day_without_pause=true')


def test_released_zero_claim_success_confirms_today(tmp_path):
    store = SQLiteStorage(tmp_path / 'confirmed-next-day.sqlite3')
    _seed_checkin(store, 0)
    observed = JoinQuantKeeper._interpret(response(
        200, json.dumps({'code': '00000', 'data': {'taskList': {'login': {'status': 1}}}})),
        now=OCTOBER_11)
    assert observed.status is KeepStatus.SUCCESS
    page = FakePage([observed])
    page.guard = SimpleNamespace(counts=Counter({'/credits/task/receiveCredits': 0}), failure=None)
    context = KeeperContext(
        flame_id='joinquant-primary', account_id='primary',
        settings={'mode': 'browser', 'task': 'checkin', 'browser_profile_dir': '/synthetic-profile',
                  'browser_headless': True},
        capabilities=CapabilityRegistry({'browser': FakeBrowser()}),
        secrets=StaticSecrets(AssertionError('the browser flow must not read secrets')),
        storage=store, now=OCTOBER_11, clock=FixedClock(OCTOBER_11),
    )
    result = JoinQuantBrowserKeeper(
        page_factory=lambda *_args, **_kwargs: page, sleeper=lambda _seconds: None,
    ).keep(context)
    assert result.status is KeepStatus.SUCCESS
    assert result.metadata.get('unconfirmed_reason') != 'action_confirmation_rejected'
    assert 'action_confirmation_rejected' not in json.dumps(result.metadata)
    assert result.metadata['automatic_action'] is False
    assert result.metadata['stage_counts']['claim'] == 0
    assert page.challenges == 0
    action = store.get_action('joinquant', 'primary', 'checkin')
    assert action.state == 'confirmed' and action.day == '2026-10-11'
    assert _archives(store) == 0
    print('observation zero_claim_2026-10-10_success_confirms_2026-10-11=true')


def test_unknown_archive_stays_paused_and_pending_day_does_not_move(tmp_path):
    from tests.test_action_archive import NOW, setup
    store, _flame, args = setup(tmp_path)
    unknown = store.archive_action(**{**args, 'disposition': 'unknown', 'run_id': None, 'review': None})
    assert unknown['disposition'] == 'unknown'
    original = store.get_action('joinquant', 'primary', 'reading')
    assert original.state == 'pending' and original.day == '2026-10-07'
    assert store.is_paused('joinquant', 'primary', 'reading') is True
    assert execution_paused(store, 'joinquant', 'primary', 'reading', now=NOW) is True
    assert store.is_paused('joinquant', 'primary', 'reading') is True
    page = FakePage([KeepResult(KeepStatus.SUCCESS, 'already done')])
    page.guard = SimpleNamespace(counts=Counter({'/credits/task/receiveCredits': 0}), failure=None)
    context = KeeperContext(
        flame_id='reading', account_id='primary',
        settings={'mode': 'browser', 'task': 'reading',
                  'article_url': 'https://www.joinquant.com/view/community/detail/12345',
                  'browser_profile_dir': '/synthetic-profile', 'browser_headless': True},
        capabilities=CapabilityRegistry({'browser': FakeBrowser()}),
        secrets=StaticSecrets(AssertionError('the browser flow must not read secrets')),
        storage=store, now=NOW, clock=FixedClock(NOW),
    )
    result = JoinQuantBrowserKeeper(
        page_factory=lambda *_args, **_kwargs: page, sleeper=lambda _seconds: None,
    ).keep(context)
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.metadata['reason'] == 'claim_unconfirmed'
    assert result.metadata['unconfirmed_reason'] == 'pending_different_day'
    assert result.metadata['automation_paused'] is True
    assert result.metadata['stage_counts']['claim'] == 0
    assert page.challenges == 0
    action = store.get_action('joinquant', 'primary', 'reading')
    assert action.state == 'pending' and action.day == '2026-10-07' and action.started_at == original.started_at
    assert store.is_paused('joinquant', 'primary', 'reading') is True
    print('observation unknown_archive_stays_paused_and_pending_day_unchanged=true')
