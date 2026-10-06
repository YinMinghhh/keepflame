"""Independent counterexamples; fake pages and a new synthetic SQLite only."""

from contextlib import contextmanager
from datetime import datetime, time, timezone
from types import SimpleNamespace
import json
import time as monotonic_time

import pytest

from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.cli import _control
from keepflame.core.config import AppConfig, FlameConfig, ScheduleConfig, StorageConfig
from keepflame.flames.joinquant.browser import JoinQuantPage
from keepflame.login import LoginCoordinator
from keepflame.storage import SQLiteStorage


NOW = datetime(2026, 10, 6, 2, tzinfo=timezone.utc)
ORIGIN = 'https://www.joinquant.com'
OPTIONS = dict(profile_dir='/synthetic/profile', account_id='synthetic', origin=ORIGIN)


class Page:
    def __init__(self, context):
        self.context, self.closed, self.fail_close = context, False, False

    def close(self):
        if self.fail_close:
            raise RuntimeError('synthetic close failure')
        self.closed = True

    def is_closed(self): return self.closed
    def goto(self, *args, **kwargs): pass
    def wait_for_timeout(self, *args): pass


class Context:
    def __init__(self):
        self.all_pages, self.handlers, self.live_pages_at_unroute = [], [], []
        self.closed = False

    @property
    def pages(self): return [page for page in self.all_pages if not page.closed]

    def new_page(self):
        page = Page(self)
        self.all_pages.append(page)
        return page

    def route(self, _pattern, handler): self.handlers.append(handler)

    def unroute(self, _pattern, handler):
        self.live_pages_at_unroute.append(len(self.pages))
        self.handlers.remove(handler)


class Browser:
    def __init__(self): self.launches = []

    @contextmanager
    def session(self, **kwargs):
        context = Context()
        self.launches.append(context)
        try:
            yield SimpleNamespace(page=context.new_page())
        finally:
            context.closed = True


def setup(tmp_path, browser=None):
    flame = FlameConfig('checkin', 'joinquant', 'synthetic', False,
                        ScheduleConfig('Asia/Shanghai', time(9), 60, time(20), time(23)),
                        {'mode': 'browser', 'browser_profile_dir': OPTIONS['profile_dir']})
    config = AppConfig(StorageConfig(tmp_path / 'synthetic.sqlite3'), (flame,), tmp_path / 'synthetic.toml')
    store = SQLiteStorage(config.storage.path)
    source = browser or Browser()
    pool = BrowserPool(source)
    keeper = SimpleNamespace(browser_origin=ORIGIN, login_url=ORIGIN+'/user/login',
                             login_blocked_paths=('/credits/', '/common/verifyCode/'))
    registry = SimpleNamespace(resolve=lambda _name: keeper)
    clock = SimpleNamespace(now=lambda: NOW)
    controller = LoginCoordinator(config, store, registry, pool, clock=clock)
    return controller, pool, source, store, flame


def observation(tmp_path, name, value):
    (tmp_path / (name + '.json')).write_text(json.dumps(value, sort_keys=True) + '\n')


def test_restart_must_not_reopen_login_after_window_open_before_state_commit(tmp_path):
    controller, pool, browser, store, flame = setup(tmp_path)
    request = store.request_login(flame.keeper, flame.account_id, flame.id, NOW)
    original = pool.begin_login

    class Crash(BaseException): pass

    def open_then_crash(**kwargs):
        original(**kwargs)
        raise Crash()

    pool.begin_login = open_then_crash
    with pytest.raises(Crash):
        controller.tick()
    state_at_crash = store.login_requests()[0]['state']
    pool.close()
    second_browser = Browser()
    second_pool = BrowserPool(second_browser)
    restarted = LoginCoordinator(controller.config, store, controller.registry, second_pool, clock=controller.clock)
    try:
        restarted.tick()
        total = len(browser.launches) + len(second_browser.launches)
        observation(tmp_path, 'login_crash', {'state_at_crash': state_at_crash, 'contexts_started': total,
                                             'same_request_id': store.login_requests(include_finished=True)[0]['id'] == request})
        assert total == 1, 'the same request reopened a login window after its owning process was lost'
    finally:
        second_pool.close()


def test_login_gate_must_survive_page_close_failure_or_context_must_close(tmp_path):
    controller, pool, browser, store, flame = setup(tmp_path)
    request = store.request_login(flame.keeper, flame.account_id, flame.id, NOW)
    controller.tick()
    context = browser.launches[0]
    context.pages[0].fail_close = True
    try:
        with pytest.raises(RuntimeError):
            pool.end_login(OPTIONS['profile_dir'])
        slot = pool._contexts.get(OPTIONS['profile_dir'])
        observation(tmp_path, 'close_failure', {'live_pages': len(context.pages), 'gate_count': len(context.handlers),
            'context_closed': context.closed, 'login_slot_cleared': slot is None or slot.login_page is None})
        assert context.closed or context.handlers, 'a live login page survived with its operation gate removed'
    finally:
        pool.close()


def test_task_gate_must_not_be_removed_while_popup_remains_alive(tmp_path):
    _, pool, browser, _, _ = setup(tmp_path)
    try:
        with pool.open(**OPTIONS) as page:
            gateway = JoinQuantPage(page, deadline=monotonic_time.monotonic() + 60)
            context = page.context
            context.new_page()  # Model an about:blank popup owned by the same task context.
            gateway.close()
            live_at_unroute = list(context.live_pages_at_unroute)
        observation(tmp_path, 'popup_cleanup', {'live_pages_at_gate_removal': live_at_unroute,
                                               'pages_after_pool_cleanup': len(context.pages)})
        assert live_at_unroute == [0], 'plugin gate was removed before the pool closed all task-owned pages'
    finally:
        pool.close()


def test_queued_login_pause_must_be_atomic_with_request_not_recreated_after_confirm(tmp_path, monkeypatch):
    controller, pool, browser, store, flame = setup(tmp_path)
    original = store.request_login

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, zone=None): return NOW

    monkeypatch.setattr('keepflame.cli.datetime', ClockDateTime)

    def insert_then_other_process_completes(*args):
        identifier = original(*args)
        # A legal process interleaving between request_login's commit and CLI pause.
        controller.tick()
        assert store.respond_login(identifier, confirm=True, now=NOW)
        controller.tick()
        return identifier

    store.request_login = insert_then_other_process_completes
    args = SimpleNamespace(command='login', confirm=None, cancel=None, flame=flame.id)
    try:
        _control(SimpleNamespace(config=controller.config, storage=store), args)
        paused = store.is_paused(flame.keeper, flame.account_id, flame.id)
        finished = store.login_requests(include_finished=True)[0]['state']
        observation(tmp_path, 'login_pause_race', {'login_state': finished, 'account_paused': paused,
                                                  'active_requests': len(store.login_requests())})
        assert not paused, 'a completed login was paused again by the request-producing CLI'
    finally:
        pool.close()
