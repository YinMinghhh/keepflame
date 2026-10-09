"""No browser or network by default; startup ordering and existing owner semantics."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from keepflame.capabilities.browser import BrowserUnavailable, IsolatedBrowser
from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.cli import main
from keepflame.core.config import ConfigError
from keepflame.runtime import build_runtime
from tests.test_browser_session_handoff import enroll
from tests.test_config import write_config
from tests.test_runtime_control import Context, Page, Browser, OPTIONS


def restore_driver(monkeypatch, *, cleanup_error=False):
    events = []
    state = {'barrier': False}

    @contextmanager
    def barrier():
        state['barrier'] = True
        events.append('barrier_started')
        try:
            yield ['--restore-last-session', '--synthetic-barrier']
        finally:
            events.append('barrier_ended')
            state['barrier'] = False

    class RestoredPage(Page):
        def close(self):
            events.append('restored_page_close')
            # Startup denial is already installed, forwarding is not.
            blocked = []
            context.handlers[-1](SimpleNamespace(abort=lambda _:blocked.append(True)))
            assert blocked == [True]
            if cleanup_error:
                raise RuntimeError('synthetic cleanup failure')
            super().close()

    context = Context()
    old = RestoredPage(context); context.all_pages.append(old)
    context.set_default_timeout = lambda _: None
    context.route_web_socket = lambda *_: None
    def close():
        assert state['barrier']
        context.closed = True
        events.append('context_closed')
    context.close = close

    def launch(*args, **kwargs):
        assert state['barrier'] and '--restore-last-session' in kwargs['args']
        assert 'proxy' not in kwargs and 'ignore_default_args' not in kwargs
        events.append('launch')
        return context

    @contextmanager
    def driver():
        class Connection:
            def _send_message_to_server(self, object, method, params, timeout, no_reply=False):
                return None
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch),
                              _impl_obj=SimpleNamespace(_connection=Connection()))
    monkeypatch.setattr('keepflame.capabilities.browser_restore.restore_barrier', barrier)
    monkeypatch.setattr('keepflame.capabilities.browser_restore.version', lambda _: '1.63.0')
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=driver))
    return context, old, state, events


def request(context, page, *, status=200, url='https://127.0.0.1/status'):
    results = []
    response = SimpleNamespace(status=status, dispose=lambda:results.append('disposed'))
    def fetch(**kwargs):
        assert kwargs == dict(timeout=10000, max_redirects=0, max_retries=0)
        results.append('fetched')
        return response
    route = SimpleNamespace(request=SimpleNamespace(url=url, frame=SimpleNamespace(page=page)),
                            fetch=fetch, fulfill=lambda **_:results.append('fulfilled'),
                            abort=lambda _:results.append('blocked'))
    context.handlers[0](route)
    return results


def test_restore_closes_old_pages_before_bridge_and_never_admits_popups(monkeypatch, tmp_path):
    context, old, state, events = restore_driver(monkeypatch)
    with IsolatedBrowser(restore_session=True).session(**enroll(tmp_path)) as owned:
        assert old.is_closed() and state['barrier']
        assert request(context, old) == ['blocked']
        popup = context.new_page()
        assert request(context, popup) == ['blocked']
        assert request(context, owned.page) == ['fetched', 'fulfilled', 'disposed']
        assert request(context, owned.page, status=302) == ['fetched', 'blocked', 'disposed']
        assert request(context, owned.page, url='http://127.0.0.1/status') == ['blocked']
        assert request(context, owned.page, url='https://other.invalid/status') == ['blocked']
        # A genuine new task page needs explicit admission from the owner.
        owned.permit_page(popup)
        assert request(context, owned.page) == ['blocked']
        assert request(context, popup) == ['fetched', 'fulfilled', 'disposed']
        popup.close()
        assert request(context, popup) == ['blocked']
    assert events == ['barrier_started', 'launch', 'restored_page_close', 'context_closed', 'barrier_ended']


def test_restore_cleanup_failure_keeps_deny_gate_until_context_is_closed(monkeypatch, tmp_path):
    context, _, state, events = restore_driver(monkeypatch, cleanup_error=True)
    with pytest.raises(BrowserUnavailable, match='browser restore startup failed'):
        with IsolatedBrowser(restore_session=True).session(**enroll(tmp_path)):
            pytest.fail('must not yield a session')
    assert context.closed and not state['barrier']
    assert len(context.handlers) == 1
    assert events[-2:] == ['context_closed', 'barrier_ended']


def test_pool_task_error_preserves_idle_context_and_new_page_admission(monkeypatch, tmp_path):
    context, _, _, _ = restore_driver(monkeypatch)
    pool = BrowserPool(IsolatedBrowser(restore_session=True))
    options = enroll(tmp_path)
    try:
        with pytest.raises(ValueError, match='ordinary task failure'):
            with pool.open(**options) as first:
                assert request(context, first) == ['fetched', 'fulfilled', 'disposed']
                raise ValueError('ordinary task failure')
        assert context.pages == [] and not context.closed and pool.close_reason is None
        assert request(context, first) == ['blocked']
        with pool.open(**options) as second:
            assert second is not first and second.context is context
            assert request(context, second) == ['fetched', 'fulfilled', 'disposed']
        assert context.pages == [] and not context.closed
    finally:
        pool.close(reason='user_stop')
    assert context.closed and pool.close_reason == 'user_stop'
    pool.close()
    assert pool.close_reason == 'user_stop'


def test_pool_cleanup_error_stops_owned_context_instead_of_reusing_it():
    source = Browser(); pool = BrowserPool(source)
    with pytest.raises(BrowserUnavailable, match='browser task cleanup failed'):
        with pool.open(**OPTIONS) as page:
            def fail_close():
                raise RuntimeError('synthetic security cleanup failure')
            page.close = fail_close
    assert source.launches[0].closed and pool.close_reason == 'security_cleanup_failed'
    with pytest.raises(BrowserUnavailable):
        with pool.open(**OPTIONS):
            pytest.fail('must not automatically restart')


def test_restore_cli_is_opt_in_daemon_only_and_dry_run(monkeypatch, tmp_path, capsys):
    config = write_config(tmp_path)
    def forbidden(*_args, **_kwargs):
        pytest.fail('dry-run must not initialize storage or browser')
    monkeypatch.setattr('keepflame.cli.build_runtime', forbidden)
    assert main(['run', '--restore-browser-session', '--config', str(config)]) == 0
    assert json.loads(capsys.readouterr().out)['browser_opened'] is False
    with pytest.raises(SystemExit):
        main(['run-once', '--restore-browser-session', '--config', str(config)])
    assert not (tmp_path/'state').exists()


def test_runtime_restore_opt_in_does_not_launch_or_change_injected_browser(tmp_path):
    config = write_config(tmp_path)
    runtime = build_runtime(config, stream=io.StringIO(), restore_browser_session=True)
    assert runtime.owned_browser.browser.restore_session is True
    assert not runtime.owned_browser._contexts
    runtime.close()
    with pytest.raises(ConfigError, match='runtime-owned browser'):
        build_runtime(config, browser=object(), restore_browser_session=True)


@pytest.mark.parametrize('reason', ['program_exit', 'user_stop', 'owner_error'])
def test_owner_close_reason_and_idle_lifecycle(tmp_path, reason):
    stream = io.StringIO()
    runtime = build_runtime(write_config(tmp_path), stream=stream, restore_browser_session=True)
    try:
        with runtime.daemon_session():
            assert not runtime._closed
            if reason == 'user_stop':
                runtime.request_stop()
            elif reason == 'owner_error':
                raise ValueError('synthetic owner failure')
    except ValueError:
        assert reason == 'owner_error'
    stopped = [json.loads(line) for line in stream.getvalue().splitlines()
               if json.loads(line)['event'] == 'daemon_owner_stopped']
    assert stopped[0]['close_reason'] == reason
    assert runtime.owned_browser.close_reason == reason


@pytest.mark.skipif(os.environ.get('KEEPFLAME_SESSION_RESTORE') != '1',
                    reason='explicit four-process local TLS/Chrome fixture only')
def test_native_restore_across_processes_and_startup_network_barrier(tmp_path):
    artifact = tmp_path/'restore-evidence.json'
    subprocess.run([sys.executable, str(Path(__file__).with_name('session_restore_probe.py')),
                    '--output', str(artifact)], check=True, timeout=190)
    result = json.loads(artifact.read_text())
    assert not result['real_profile_opened']
    assert result['cookie_export_calls'] == result['real_service_requests'] == 0
    assert result['temporary_profiles_cleaned']
    assert len(result['protected_server_requests']) == 4
