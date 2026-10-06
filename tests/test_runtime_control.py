from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from keepflame.capabilities.browser import BrowserUnavailable, ProfileNotAuthorized
from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.cli import main
from keepflame.core.config import ConfigError, load_config
from keepflame.core.models import HumanAction, KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.protocols import AutomationPaused
from keepflame.core.service import SchedulerService
from keepflame.login import LoginCoordinator
from keepflame.storage import SQLiteStorage
from tests.helpers import FakeNotifier, FixedClock, MemoryStorage, SequenceKeeper, StaticRegistry, StaticSecrets
from tests.test_config import BASE_CONFIG, write_config

NOW = datetime(2026, 10, 6, 2, tzinfo=timezone.utc)


def test_default_dry_run_never_constructs_runtime(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('dry-run must not initialize storage, browser or secrets')
    monkeypatch.setattr('keepflame.cli.build_runtime', forbidden)
    path = write_config(tmp_path)
    for command in ('run', 'run-once', 'login', 'resume'):
        assert main([command, '--config', str(path)]) == 0
        assert json.loads(capsys.readouterr().out)['execution_enabled'] is False
    assert not (tmp_path / 'state').exists()


def test_omitted_enabled_is_off_and_duplicate_semantic_tasks_rejected(tmp_path):
    assert not load_config(write_config(tmp_path, BASE_CONFIG.replace('enabled = true', ''))).flames[0].enabled
    duplicate = BASE_CONFIG + '\n[[flames]]' + BASE_CONFIG.split('[[flames]]', 1)[1].replace('id = "joinquant-primary"', 'id = "alias"')
    with pytest.raises(ConfigError, match='duplicate keeper/account/task'):
        load_config(write_config(tmp_path, duplicate))


def test_action_intent_survives_restart_and_next_day_and_resume(tmp_path):
    path = tmp_path / 'state.sqlite3'
    first = SQLiteStorage(path)
    assert first.reserve_action('keeper', 'primary', 'reading', '2026-10-06', NOW)
    second = SQLiteStorage(path)
    assert not second.reserve_action('keeper', 'primary', 'reading', '2026-10-07', NOW + timedelta(days=1))
    second.pause('keeper', 'primary', 'reading-flame', 'claim_unconfirmed', NOW)
    second.resume('keeper', 'primary', 'reading-flame')
    assert second.get_action('keeper', 'primary', 'reading').state == 'pending'
    assert second.reserve_action('keeper', 'primary', 'checkin', '2026-10-06', NOW)
    assert second.reserve_action('keeper', 'other', 'reading', '2026-10-06', NOW)
    second.confirm_action('keeper', 'primary', 'reading', NOW)
    assert not second.reserve_action('keeper', 'primary', 'reading', '2026-10-06', NOW)
    assert not second.reserve_action('keeper', 'primary', 'reading', '2026-10-05', NOW)
    assert second.reserve_action('keeper', 'primary', 'reading', '2026-10-07', NOW + timedelta(days=1))


def test_competing_action_intents_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'state.sqlite3'
    first, second = SQLiteStorage(path), SQLiteStorage(path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda store: store.reserve_action('k', 'a', 'task', '2026-10-06', NOW), (first, second)))
    assert sorted(results) == [False, True]


def test_account_pause_stops_other_flame_and_later_ticks(tmp_path):
    config = load_config(write_config(tmp_path))
    config = replace(config, flames=(*config.flames, replace(config.flames[0], id='reading', settings={'task':'reading'})))
    storage = MemoryStorage()
    keeper = SequenceKeeper(KeepResult(KeepStatus.WAITING_HUMAN, 'expired',
        metadata={'automation_paused': True, 'pause_account': True, 'reason':'login_required'},
        human_action=HumanAction('login', 'Login', 'Use the owned login window')))
    runner = Orchestrator(storage=storage, registry=StaticRegistry(keeper), capabilities=None,
                          secrets=StaticSecrets('unused'), notifier=FakeNotifier(), clock=FixedClock(NOW))
    service = SchedulerService(config=config, storage=storage, orchestrator=runner, clock=FixedClock(NOW))
    assert len(service.run_due(now=NOW)[0]) == 1
    assert service.run_due(now=NOW + timedelta(days=1))[0] == []
    assert len(storage.runs) == 1
    with pytest.raises(AutomationPaused):
        runner.run_flame(config.flames[1], now=NOW)


class Page:
    def __init__(self, context): self.context, self.closed = context, False
    def close(self): self.closed = True
    def is_closed(self): return self.closed
    def goto(self, url, **kwargs): self.context.visits.append(url)
    def wait_for_timeout(self, _): pass


class Context:
    def __init__(self): self.all_pages, self.handlers, self.visits, self.closed = [], [], [], False
    @property
    def pages(self): return [p for p in self.all_pages if not p.closed]
    def new_page(self):
        page = Page(self); self.all_pages.append(page); return page
    def route(self, _, handler): self.handlers.append(handler)
    def unroute(self, _, handler): self.handlers.remove(handler)


class Browser:
    def __init__(self): self.launches = []
    @contextmanager
    def session(self, **kwargs):
        context = Context(); self.launches.append(context)
        try: yield SimpleNamespace(page=context.new_page())
        finally: context.closed = True


OPTIONS = dict(profile_dir='/synthetic/profile', account_id='synthetic', origin='https://fixture.invalid')


def test_pool_keeps_context_but_closes_each_task_and_cannot_cross_accounts():
    browser = Browser(); pool = BrowserPool(browser)
    with pool.open(**OPTIONS) as first:
        context = first.context
    assert first.closed and not context.closed
    with pool.open(**OPTIONS) as second:
        assert second is not first and second.context is context
        context.new_page()  # Unexpected popup is closed with the task.
        with pytest.raises(BrowserUnavailable):
            with pool.open(**OPTIONS): pass
    assert context.pages == [] and len(browser.launches) == 1
    with pytest.raises(ProfileNotAuthorized):
        with pool.open(**{**OPTIONS, 'account_id':'different'}): pass
    pool.close()
    assert context.closed
    with pytest.raises(BrowserUnavailable):
        with pool.open(**OPTIONS): pass


def login_coordinator(tmp_path):
    config = load_config(write_config(tmp_path))
    flame = replace(config.flames[0], account_id='synthetic', settings={'browser_profile_dir':OPTIONS['profile_dir']})
    config = replace(config, flames=(flame,))
    store = SQLiteStorage(tmp_path / 'control.sqlite3')
    browser = Browser(); pool = BrowserPool(browser)
    keeper = SimpleNamespace(browser_origin=OPTIONS['origin'], login_url=OPTIONS['origin']+'/login',
                             login_blocked_paths=('/credits/', '/challenge/'))
    clock = FixedClock(NOW)
    controller = LoginCoordinator(config, store, StaticRegistry(keeper), pool, clock=clock)
    return controller, store, pool, browser, flame, clock


def test_login_confirm_keeps_context_and_never_clears_pending_action(tmp_path):
    controller, store, pool, browser, flame, clock = login_coordinator(tmp_path)
    request = store.request_login(flame.keeper, flame.account_id, flame.id, NOW)
    store.reserve_action(flame.keeper, flame.account_id, 'reading', '2026-10-06', NOW)
    controller.tick()
    assert store.login_requests()[0]['state'] == 'waiting'
    assert controller.blocked(flame) and store.is_paused(flame.keeper, flame.account_id, flame.id)
    context = browser.launches[0]
    attempts = []
    context.handlers[0](SimpleNamespace(request=SimpleNamespace(url=OPTIONS['origin']+'/credits/receive'),
                                        abort=lambda reason: attempts.append('blocked'), fallback=lambda: attempts.append('allowed')))
    assert attempts == ['blocked']
    assert store.respond_login(request, confirm=True, now=NOW)
    controller.tick()
    assert context.pages == [] and context.handlers == [] and not context.closed
    assert not store.is_paused(flame.keeper, flame.account_id, flame.id)
    assert store.get_action(flame.keeper, flame.account_id, 'reading').state == 'pending'
    with pool.open(**OPTIONS) as page: assert page.context is context
    assert len(browser.launches) == 1
    pool.close()


@pytest.mark.parametrize('outcome', ['cancel', 'expire', 'restart'])
def test_incomplete_login_stays_paused_without_reopening(tmp_path, outcome):
    controller, store, pool, browser, flame, clock = login_coordinator(tmp_path)
    request = store.request_login(flame.keeper, flame.account_id, flame.id, NOW)
    controller.tick()
    if outcome == 'cancel': store.respond_login(request, confirm=False, now=NOW)
    elif outcome == 'expire': clock.current += timedelta(minutes=16)
    else:
        pool.close()
        controller = LoginCoordinator(controller.config, store, controller.registry, BrowserPool(Browser()), clock=clock)
    controller.tick()
    assert store.login_requests() == []
    assert store.is_paused(flame.keeper, flame.account_id, flame.id)
    assert len(browser.launches) == 1
    pool.close()


@pytest.mark.skipif(__import__('os').environ.get('KEEPFLAME_SYNTHETIC_BROWSER') != '1', reason='explicit synthetic Chrome only')
def test_native_cookie_survives_formal_login_and_repeated_task_pages(tmp_path, monkeypatch):
    from playwright.sync_api import BrowserType, BrowserContext
    from keepflame.capabilities.browser import IsolatedBrowser
    profile = tmp_path / 'synthetic-profile'
    profile.mkdir(mode=0o700)
    marker = profile / '.keepflame-profile.json'
    marker.write_text(json.dumps({'version':1, 'account_id':'synthetic', 'purpose':'keepflame'}))
    marker.chmod(0o600)
    options = {**OPTIONS, 'profile_dir':str(profile)}
    native_launch = BrowserType.launch_persistent_context
    launches, observations, attempted_external = [], [], []

    def launch(self, *args, **kwargs):
        kwargs['args'].append('--host-resolver-rules=MAP * ~NOTFOUND')
        launches.append(True)
        return native_launch(self, *args, **kwargs)

    def no_export(*args, **kwargs): pytest.fail('cookie export is forbidden')
    monkeypatch.setattr(BrowserType, 'launch_persistent_context', launch)
    monkeypatch.setattr(BrowserContext, 'cookies', no_export)
    monkeypatch.setattr(BrowserContext, 'storage_state', no_export)

    class SyntheticBrowser:
        @contextmanager
        def session(self, **kwargs):
            with IsolatedBrowser().session(**kwargs) as owned:
                def fixture(route):
                    url = route.request.url
                    if url == OPTIONS['origin']+'/login':
                        route.fulfill(status=200, headers={'Content-Type':'text/html', 'Set-Cookie':'synthetic_auth=fixture; Path=/; Secure; HttpOnly; SameSite=Lax'}, body='<title>Synthetic login</title>')
                    elif url == OPTIONS['origin']+'/status':
                        authenticated = 'synthetic_auth=fixture' in (route.request.header_value('cookie') or '')
                        observations.append(authenticated)
                        route.fulfill(status=200, headers={'Content-Type':'application/json'}, body=json.dumps({'authenticated':authenticated}))
                    else:
                        attempted_external.append(url)
                        route.abort('blockedbyclient')
                owned.page.context.route('**/*', fixture)
                yield owned

    pool = BrowserPool(SyntheticBrowser(), headless=True)
    config = load_config(write_config(tmp_path))
    flame = replace(config.flames[0], account_id='synthetic', settings={'browser_profile_dir':str(profile)})
    config = replace(config, flames=(flame,))
    store = SQLiteStorage(tmp_path / 'native.sqlite3')
    keeper = SimpleNamespace(browser_origin=OPTIONS['origin'], login_url=OPTIONS['origin']+'/login', login_blocked_paths=('/credits/',))
    controller = LoginCoordinator(config, store, StaticRegistry(keeper), pool, clock=FixedClock(NOW))
    request = store.request_login(flame.keeper, flame.account_id, flame.id, NOW)
    pages = []
    try:
        controller.tick()
        assert store.login_requests()[0]['state'] == 'waiting'
        assert store.respond_login(request, confirm=True, now=NOW)
        controller.tick()
        for _ in range(2):
            with pool.open(**options) as page:
                pages.append(page)
                response = page.goto(OPTIONS['origin']+'/status')
                assert response.json()['authenticated'] is True
            assert page.is_closed()
        assert pages[0].context is pages[1].context
        assert len(launches) == 1 and observations == [True, True]
        assert attempted_external == []
    finally:
        pool.close()
    assert all(page.is_closed() for page in pages)


def test_cross_day_confirmation_blocks_another_write_on_confirmation_day(tmp_path):
    store = SQLiteStorage(tmp_path / 'cross-day.sqlite3')
    assert store.reserve_action('k', 'a', 'reading', '2026-10-06', NOW)
    next_day = NOW + timedelta(days=1)
    store.confirm_action('k', 'a', 'reading', next_day, day='2026-10-07')
    assert store.get_action('k', 'a', 'reading').day == '2026-10-07'
    assert not store.reserve_action('k', 'a', 'reading', '2026-10-07', next_day)
