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
    for command in ('run', 'run-once', 'login', 'resume', 'reload'):
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
    second.confirm_action('keeper', 'primary', 'reading', NOW, day='2026-10-06')
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


def test_visible_request_overrides_headless_pool():
    class RecordingBrowser:
        def __init__(self):
            self.launches = []

        @contextmanager
        def session(self, **kwargs):
            self.launches.append(kwargs['headless'])
            context = Context()
            try:
                yield SimpleNamespace(page=context.new_page())
            finally:
                context.closed = True

    browser = RecordingBrowser()
    pool = BrowserPool(browser, headless=True)
    with pool.open(**OPTIONS, headless=False):
        pass
    assert browser.launches == [False] and pool.launch_count == 1
    with pool.open(**OPTIONS):
        pass
    assert pool.launch_count == 1 and browser.launches == [False]
    other = RecordingBrowser()
    default_pool = BrowserPool(other, headless=True)
    with default_pool.open(**OPTIONS):
        pass
    assert other.launches == [True] and default_pool.launch_count == 1
    print('observation pool_open_headless_false_launches_visible=true')
    print('observation pool_default_open_uses_constructor_headless=true')


def test_one_owner_serves_both_tasks_and_second_start_does_not_launch(tmp_path):
    from keepflame.capabilities.profile_owner import ProfileInUse, ProfileOwner, browser_profiles, prepare_visible_owner
    from keepflame.core.plugins import EntryPointKeeperRegistry

    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    profile = (home / 'profile').resolve()
    profile.mkdir(mode=0o700)
    profile.chmod(0o700)
    marker = profile / '.keepflame-profile.json'
    marker.write_text(json.dumps({'version': 1, 'account_id': 'primary', 'purpose': 'keepflame'}))
    marker.chmod(0o600)
    second = '''
[[flames]]
id = "joinquant-reading-primary"
keeper = "joinquant"
account_id = "primary"
enabled = true

[flames.schedule]
timezone = "Asia/Shanghai"
first_check = "09:00"
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"

[flames.settings]
mode = "browser"
task = "reading"
browser_profile_dir = "%s"
browser_headless = true
article_url = "https://www.joinquant.com/view/community/detail/34862"
''' % profile
    checkin = BASE_CONFIG.replace('path = "state/keepflame.sqlite3"', 'path = "owner.sqlite3"').replace(
        'cookie_file = "/run/secrets/joinquant.cookie"',
        'mode = "browser"\ntask = "checkin"\nbrowser_profile_dir = "%s"\nbrowser_headless = true' % profile,
    )
    config = load_config(write_config(tmp_path, checkin + second))
    profiles = browser_profiles(config, EntryPointKeeperRegistry())
    assert profiles == [(str(profile), 'primary', 'https://www.joinquant.com')]
    launches = []

    class Pool:
        def __init__(self):
            self.launch_count = 0

        def open_visible(self, **kwargs):
            launches.append(kwargs)
            self.launch_count += 1

    owner = ProfileOwner()
    owner.acquire(profiles)
    runtime = SimpleNamespace(owned_browser=Pool())
    assert prepare_visible_owner(runtime, profiles) == 1
    assert len(launches) == 1 and runtime.owned_browser.launch_count == 1
    with pytest.raises(ProfileInUse):
        ProfileOwner().acquire(profiles)
    assert len(launches) == 1
    owner.release()
    print('observation one_visible_owner_for_checkin_and_reading=true')
    print('observation second_start_refused_without_launch=true')


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
    context.handlers[-1](SimpleNamespace(request=SimpleNamespace(url=OPTIONS['origin']+'/credits/receive'),
                                        abort=lambda reason: attempts.append('blocked'), fallback=lambda: attempts.append('allowed')))
    assert attempts == ['blocked']
    assert store.respond_login(request, confirm=True, now=NOW)
    controller.tick()
    assert context.pages == [] and len(context.handlers) == 1 and not context.closed
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
    if outcome == 'expire':
        assert store.login_requests()[0]['state'] == 'waiting'
        assert browser.launches[0].pages
    else:
        assert store.login_requests() == []
    assert store.is_paused(flame.keeper, flame.account_id, flame.id)
    assert len(browser.launches) == 1
    pool.close()


def _seed_waiting(store, flame, when, next_action):
    from keepflame.core.models import BusinessTarget, FlameState, KeepResult, KeepStatus, RunRecord
    target = BusinessTarget.at(when, "Asia/Shanghai")
    result = KeepResult(KeepStatus.WAITING_HUMAN, "JoinQuant login session has expired",
                        human_action=HumanAction("REFRESH_SESSION", "登录", "在独立窗口登录"),
                        next_action_at=next_action)
    state = FlameState(flame.id, flame.account_id, KeepStatus.WAITING_HUMAN, result.message,
                       when, None, 0, when, result.human_action, next_action, None, {}, target, None)
    store.record(RunRecord("seed-" + flame.id, flame.id, flame.account_id, when, when, result, target), state)


def test_proven_login_clears_only_the_login_pause_and_dues_flames_now(tmp_path):
    from keepflame.capabilities.http import HttpResponse
    from keepflame.flames.joinquant.keeper import TASK_INFO_URL, JoinQuantKeeper
    controller, store, pool, browser, flame, clock = login_coordinator(tmp_path)
    reading = replace(flame, id="joinquant-reading-primary")
    other = replace(flame, id="other-account", account_id="other")
    disabled = replace(reading, id="disabled-reading", enabled=False)
    controller.config = replace(controller.config, flames=(flame, reading, other, disabled))
    controller.registry = StaticRegistry(JoinQuantKeeper())
    later = NOW + timedelta(days=1)
    _seed_waiting(store, flame, NOW, later)
    _seed_waiting(store, reading, NOW, later)
    store.pause(flame.keeper, flame.account_id, reading.id, "claim_unconfirmed", NOW)
    store.request_login(flame.keeper, flame.account_id, flame.id, NOW)
    controller.tick()
    context = browser.launches[0]
    assert store.login_requests()[0]["state"] == "waiting"
    login_page = pool._contexts[OPTIONS["profile_dir"]].login_page
    assert login_page is not None and not login_page.is_closed() and login_page in context.pages
    clock.current += timedelta(minutes=16)
    controller.tick()
    assert store.login_requests()[0]["state"] == "waiting"
    assert login_page in context.pages and not login_page.is_closed()
    assert store.pause_reason(flame.keeper, flame.account_id, "*") == "login_requested"
    logged_out = JoinQuantKeeper._interpret(
        HttpResponse(200, TASK_INFO_URL, {}, b'{"code":"10001","msg":"login"}'), now=clock.current)
    assert logged_out.status is KeepStatus.WAITING_HUMAN
    assert JoinQuantKeeper.login_proven(200, b'{"code":"10001","msg":"login"}') is False
    context.proof_body = lambda _url: (200, b'{"code":"10001","msg":"login"}')
    controller.tick()
    assert login_page in context.pages and not login_page.is_closed()
    assert store.pause_reason(flame.keeper, flame.account_id, "*") == "login_requested"
    assert store.get_state(flame.id, flame.account_id).next_action_at == later
    context.proof_body = lambda _url: (200, b'{"code":"00000","data":{"taskList":{"login":{"status":0}}}}')
    controller.tick()
    proven_at = clock.current.astimezone(timezone.utc)
    assert store.pause_reason(flame.keeper, flame.account_id, "*") is None
    assert store.pause_reason(flame.keeper, flame.account_id, reading.id) == "claim_unconfirmed"
    assert store.is_paused(flame.keeper, flame.account_id, reading.id)
    assert not store.is_paused(flame.keeper, flame.account_id, flame.id)
    assert store.get_state(flame.id, flame.account_id).next_action_at == proven_at
    assert store.get_state(reading.id, reading.account_id).next_action_at == proven_at
    assert store.get_state(other.id, other.account_id) is None
    assert store.get_state(disabled.id, disabled.account_id) is None
    assert store.login_requests() == []
    print("observation login_proof_clears_only_login_pause=true")
    print("observation login_expiry_keeps_window_open=true")
    print("observation logged_out_stays_WAITING_HUMAN=true")
    print("observation unconfirmed_claim_pause_remains=true")
    pool.close()


def test_login_proof_does_not_clear_a_claim_account_pause(tmp_path):
    from keepflame.flames.joinquant.keeper import JoinQuantKeeper
    controller, store, pool, browser, flame, _clock = login_coordinator(tmp_path)
    controller.registry = StaticRegistry(JoinQuantKeeper())
    store.request_login(flame.keeper, flame.account_id, flame.id, NOW)
    controller.tick()
    store.pause(flame.keeper, flame.account_id, "*", "claim_unconfirmed", NOW)
    browser.launches[0].proof_body = lambda _url: (
        200, b'{"code":"00000","data":{"taskList":{"login":{"status":1}}}}')
    controller.tick()
    assert store.pause_reason(flame.keeper, flame.account_id, "*") == "claim_unconfirmed"
    assert store.is_paused(flame.keeper, flame.account_id, flame.id)
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


def test_cross_day_confirmation_preserves_original_pending_and_blocks_another_write(tmp_path):
    store = SQLiteStorage(tmp_path / 'cross-day.sqlite3')
    assert store.reserve_action('k', 'a', 'reading', '2026-10-06', NOW)
    next_day = NOW + timedelta(days=1)
    original = store.get_action('k', 'a', 'reading')
    assert not store.confirm_action('k', 'a', 'reading', next_day, day='2026-10-07')
    assert store.get_action('k', 'a', 'reading') == original
    assert not store.reserve_action('k', 'a', 'reading', '2026-10-07', next_day)



def test_pool_reverse_binding_and_idle_gate():
    browser = Browser(); pool = BrowserPool(browser)
    outcomes = []
    with pool.open(**OPTIONS) as page:
        handler = page.context.handlers[0]
        request = SimpleNamespace(frame=SimpleNamespace(page=page))
        route = SimpleNamespace(request=request, fallback=lambda:outcomes.append('allowed'), abort=lambda _:outcomes.append('blocked'))
        handler(route)
        with pytest.raises(ProfileNotAuthorized):
            with pool.open(**{**OPTIONS, 'profile_dir':'/synthetic/other'}): pass
        popup = page.context.new_page()
        route.request = SimpleNamespace(frame=SimpleNamespace(page=popup)); handler(route)
    route.request = request; handler(route)
    assert outcomes == ['allowed','blocked','blocked']
    assert len(browser.launches) == 1
    pool.close()


def test_disabled_idle_runtime_and_borrower_close_keep_the_owner_context(tmp_path):
    import io
    from keepflame.runtime import build_runtime
    path = write_config(tmp_path, BASE_CONFIG.replace('enabled = true', 'enabled = false'))
    source = Browser()
    pool = BrowserPool(source)
    runtime = build_runtime(path, stream=io.StringIO(), browser=pool)
    try:
        with pool.open(**OPTIONS) as page:
            original = page.context
        assert page.closed and not original.closed and not original.pages
        assert runtime.service.run_due(now=NOW + timedelta(days=2)) == ([], None)
        runtime.close()  # Borrowers do not own the long-lived account context.
        assert not original.closed and not original.pages
        with pool.open(**OPTIONS) as second:
            assert second.context is original
        assert second.closed and len(source.launches) == 1
    finally:
        runtime.close()
        pool.close()
    assert original.closed


def test_reload_enables_tasks_without_restarting_browser_and_rejects_db_change(tmp_path, monkeypatch):
    import io
    from keepflame.runtime import build_runtime
    monkeypatch.setattr('keepflame.runtime.SystemClock', lambda: FixedClock(NOW))
    path = write_config(tmp_path, BASE_CONFIG.replace('enabled = true', 'enabled = false'))
    source = Browser(); pool = BrowserPool(source)
    runtime = build_runtime(path, stream=io.StringIO(), browser=pool)
    try:
        with pool.open(**OPTIONS) as page: original_context = page.context
        runtime.storage.reserve_action('joinquant', 'primary', 'reading', '2026-10-06', NOW)
        path.write_text(BASE_CONFIG)
        assert runtime.config.enabled_flames() == ()
        runtime.storage.request_reload(NOW)
        runtime.poll_controls()
        assert len(runtime.config.enabled_flames()) == 1
        assert runtime.service.config is runtime.config and runtime.login.config is runtime.config
        with pool.open(**OPTIONS) as page: assert page.context is original_context
        assert len(source.launches) == 1
        assert runtime.storage.get_action('joinquant', 'primary', 'reading').state == 'pending'
        path.write_text(BASE_CONFIG.replace('state/keepflame.sqlite3', 'new.sqlite3'))
        failed_request = runtime.storage.request_reload(NOW)
        runtime.poll_controls()
        assert runtime.configuration_blocked
        assert not (tmp_path/'new.sqlite3').exists()
        outcomes = {row['id']:row for row in runtime.storage.runtime_commands(include_finished=True)}
        assert outcomes[failed_request]['state'] == 'failed' and outcomes[failed_request]['error_type'] == 'ConfigError'
        path.write_text(BASE_CONFIG)
        runtime.storage.request_reload(NOW)
        runtime.poll_controls()
        assert not runtime.configuration_blocked
    finally:
        pool.close()


@pytest.mark.parametrize('status', [200, 302, 307])
def test_owned_origin_gate_never_follows_redirects(tmp_path, monkeypatch, status):
    import sys
    from keepflame.capabilities.browser import IsolatedBrowser
    from tests.test_browser_session_handoff import enroll
    handlers, outcomes = [], []
    context = SimpleNamespace(set_default_timeout=lambda _:None, route=lambda _, handler:handlers.append(handler),
                              route_web_socket=lambda *_:None, new_page=lambda:object(), close=lambda:None)
    @contextmanager
    def driver():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a,**kw:context))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=driver))
    response = SimpleNamespace(status=status, dispose=lambda:outcomes.append('disposed'))
    def fetch(**options):
        assert options['max_redirects'] == options['max_retries'] == 0
        outcomes.append('fetched')
        return response
    route = SimpleNamespace(request=SimpleNamespace(url='https://127.0.0.1/login'), fetch=fetch,
                            fulfill=lambda **_:outcomes.append('fulfilled'), abort=lambda _:outcomes.append('blocked'))
    with IsolatedBrowser().session(**enroll(tmp_path)):
        handlers[0](route)
    assert outcomes == ['fetched', 'fulfilled' if status == 200 else 'blocked', 'disposed']
