"""Independent synthetic counterexamples for frozen commit 6368ac0; no browser."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from keepflame.core.models import FlameState, KeepStatus, RunRecord
from keepflame.capabilities.http import HttpResponse
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from keepflame.flames.joinquant.guard import SignInGuard
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN, TASK_INFO_URL, JoinQuantKeeper
from keepflame.flames.joinquant.task import task_from_settings
from keepflame.storage import SQLiteStorage


ROOT = Path(__file__).parent
ARTICLE = JOINQUANT_ORIGIN + '/view/community/detail/12345'
NOW = datetime(2026, 10, 6, 2, tzinfo=timezone.utc)
TASK = task_from_settings({'task': 'reading', 'article_url': ARTICLE})


def record(name, values):
    pass  # Assertions below are the regression evidence; do not write into source.


class Context:
    def __init__(self):
        self.pages = []
        self.handlers = []
        self.removals = []

    def route(self, pattern, callback):
        self.handlers.append(callback)

    def unroute(self, pattern, callback):
        self.removals.append([page.name for page in self.pages if not page.closed])
        self.handlers.remove(callback)


class Page:
    def __init__(self, context, name, *, fail_close=False):
        self.context, self.name = context, name
        self.closed = False
        self.fail_close = fail_close
        self.url = ARTICLE
        self.main_frame = SimpleNamespace(page=self, url=ARTICLE)
        context.pages.append(self)

    def close(self):
        if self.fail_close:
            raise RuntimeError('synthetic close failure')
        self.closed = True
        self.context.pages.remove(self)

    def is_closed(self):
        return self.closed


class NavigationRoute:
    """Redirect response offered by an in-memory transport, never real network."""
    def __init__(self, page):
        self.request = SimpleNamespace(url=ARTICLE, method='GET', resource_type='document',
                                       post_data=None, headers={}, frame=page.main_frame)
        self.fallbacks = 0
        self.fetch_options = []
        self.aborted = []
        self.delivered = []

    def fallback(self):
        self.fallbacks += 1

    def fetch(self, **options):
        self.fetch_options.append(options)
        return SimpleNamespace(status=307, headers={'location': 'https://unapproved.invalid/redirect'},
                               body=lambda: b'', dispose=lambda: None)

    def abort(self, reason):
        self.aborted.append(reason)

    def fulfill(self, **options):
        self.delivered.append(options)


def test_article_navigation_must_reject_redirect_before_browser_follows():
    context = Context()
    page = Page(context, 'task')
    guard = SignInGuard(page, deadline=time.monotonic() + 60, task='reading')
    guard.phase = 'inspect'
    guard.begin_reading(article_id='12345', article_url=ARTICLE)
    route = NavigationRoute(page)
    guard.route(route)
    record('navigation', {'fallback_calls': route.fallbacks, 'native_fetches': len(route.fetch_options),
                          'aborted': bool(route.aborted), 'failure': None if guard.failure is None else guard.failure.reason})
    assert route.fallbacks == 0, 'Navigation escapes the native no-redirect transport'
    assert route.fetch_options[0]['max_redirects'] == 0
    assert route.aborted and not route.delivered


@pytest.mark.parametrize('fail_close', [False, True])
def test_gate_must_stay_until_all_owned_pages_are_closed(fail_close):
    context = Context()
    page = Page(context, 'task', fail_close=fail_close)
    Page(context, 'popup')
    gateway = JoinQuantPage(page, deadline=time.monotonic() + 60, task=TASK)
    if fail_close:
        with pytest.raises(RuntimeError, match='synthetic close failure'):
            gateway.close()
    else:
        gateway.close()
    record('cleanup-failure' if fail_close else 'cleanup-popup', {
        'live_pages_at_unroute': context.removals,
        'guard_still_installed': gateway.guard._handler in context.handlers,
    })
    assert all(not pages for pages in context.removals), 'Gate removed while an owned page is still live'


def test_unknown_application_code_must_not_persist_response_values(tmp_path):
    sentinel = 'SYNTHETIC_PRIVATE_ACCOUNT_VALUE'
    response = HttpResponse(status=200, url=TASK_INFO_URL, headers={},
        body=json.dumps({'code': {'arbitrary_profile_value': sentinel}}).encode())
    raw = JoinQuantKeeper._interpret(response, now=NOW, task='reading')
    result = JoinQuantBrowserKeeper()._observation(SimpleNamespace(now=NOW), raw, TASK)
    assert result.status is KeepStatus.FAILED and result.metadata['automation_paused']
    storage = SQLiteStorage(tmp_path / 'synthetic.sqlite3')
    state = FlameState('reading', 'synthetic', result.status, result.message, NOW, None, 1,
                       None, None, result.next_action_at, None, result.metadata)
    storage.record(RunRecord('synthetic-run', 'reading', 'synthetic', NOW, NOW, result), state)
    leaked = sentinel in json.dumps(storage.get_state('reading', 'synthetic').metadata)
    on_disk = sentinel.encode() in storage.path.read_bytes()
    record('unknown-code', {'parser_status': result.status.value, 'paused': True,
                           'response_value_in_metadata': leaked, 'response_value_in_sqlite': on_disk})
    assert not leaked and not on_disk, 'Arbitrary response code object was stringified into persistent metadata'


def reading_result(record):
    return JoinQuantKeeper._interpret(HttpResponse(status=200, url=TASK_INFO_URL, headers={},
        body=json.dumps({'code': '00000', 'data': {'taskList': {'post_view': record}}}).encode()),
        now=NOW, task='reading')


def test_real_sqlite_pending_only_inspects_after_restart_and_next_day(tmp_path):
    path = tmp_path / 'synthetic.sqlite3'
    storage = SQLiteStorage(path)
    assert storage.reserve_action('joinquant', 'synthetic', 'reading', '2026-10-06', NOW)
    storage.pause('joinquant', 'synthetic', 'reading', 'claim_unconfirmed', NOW)
    storage.resume('joinquant', 'synthetic', 'reading')
    storage = SQLiteStorage(path)
    calls = []
    context = SimpleNamespace(now=NOW + timedelta(days=1), account_id='synthetic', storage=storage)
    gateway = SimpleNamespace(inspect=lambda _: (calls.append('inspect') or reading_result({'status': 0, 'totalCount': 1})))
    result = JoinQuantBrowserKeeper()._run(context, gateway, TASK, {'reserved': False, 'action_stage': 'inspect'})
    assert calls == ['inspect'] and result.metadata['reason'] == 'claim_unconfirmed'
    assert storage.get_action('joinquant', 'synthetic', 'reading').state == 'pending'
    gateway.inspect = lambda _: (calls.append('inspect') or reading_result({'status': 1}))
    result = JoinQuantBrowserKeeper()._run(context, gateway, TASK, {'reserved': False, 'action_stage': 'inspect'})
    assert calls == ['inspect', 'inspect'] and result.status is KeepStatus.WAITING_HUMAN
    assert result.metadata['unconfirmed_reason'] == 'pending_different_day'
    action = storage.get_action('joinquant', 'synthetic', 'reading')
    assert action.state == 'pending' and action.day == '2026-10-06'
    assert action.started_at == NOW and action.confirmed_at is None
    assert not storage.reserve_action('joinquant', 'synthetic', 'reading', '2026-10-07', context.now)
    record('durable-intent', {'inspection_only_after_restart': True, 'pending_blocks_next_day': True,
                              'current_day_completion_preserves_pending': True, 'original_action_day': action.day})


@pytest.mark.parametrize('value', ['SYNTHETIC_PRIVATE_ACCOUNT_VALUE', {'nested':'SYNTHETIC_PRIVATE_ACCOUNT_VALUE'}, 12345])
def test_unknown_codes_and_server_time_are_not_arbitrary_metadata(value):
    payload = {'code':value}
    result = JoinQuantKeeper._interpret(HttpResponse(200,TASK_INFO_URL,{},json.dumps(payload).encode()),now=NOW)
    assert result.metadata['remote_code'] == 'unknown'
    payload = {'code':'00000','data':{'currTime':'SYNTHETIC_PRIVATE_ACCOUNT_VALUE','taskList':{'login':{'status':1}}}}
    result = JoinQuantKeeper._interpret(HttpResponse(200,TASK_INFO_URL,{},json.dumps(payload).encode()),now=NOW)
    assert 'server_time' not in result.metadata
    assert 'SYNTHETIC_PRIVATE_ACCOUNT_VALUE' not in json.dumps(result.metadata)
