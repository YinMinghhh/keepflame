"""Owned in-process handoff; synthetic profiles only, no website account access."""
from contextlib import contextmanager
import json
import os
import sys
from types import SimpleNamespace

import pytest

from keepflame.capabilities.browser import BrowserUnavailable, IsolatedBrowser, ProfileNotAuthorized
from keepflame.runtime import build_runtime
from tests.test_config import BASE_CONFIG


def enroll(tmp_path):
    profile = tmp_path / 'synthetic-profile'
    profile.mkdir(mode=0o700)
    marker = profile / '.keepflame-profile.json'
    marker.write_text(json.dumps({'version': 1, 'account_id': 'synthetic', 'purpose': 'keepflame'}))
    marker.chmod(0o600)
    return dict(profile_dir=str(profile), account_id='synthetic', origin='https://127.0.0.1', headless=False)


def fake_driver(monkeypatch):
    events = []
    page = object()

    class Context:
        def set_default_timeout(self, _timeout): pass
        def route(self, *_args): pass
        def route_web_socket(self, *_args): pass
        def new_page(self): return page
        def close(self): events.append('close')

    class Driver:
        def launch_persistent_context(self, *_args, **_kwargs):
            events.append('launch')
            return Context()

    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=Driver())

    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=playwright))
    return events, page


def test_handoff_keeps_browser_open_and_is_single_use(monkeypatch, tmp_path):
    events, page = fake_driver(monkeypatch)
    options = enroll(tmp_path)
    with IsolatedBrowser().session(**options) as session:
        assert session.page is page
        with session.open(**{**options, 'headless': True}) as borrowed:
            assert borrowed is page
        assert events == ['launch']
        with pytest.raises(BrowserUnavailable):
            with session.open(**options):
                pytest.fail('a second run must not acquire the session')
    assert events == ['launch', 'close']
    with pytest.raises(BrowserUnavailable):
        with session.open(**options):
            pytest.fail('an expired session must not open a new browser')


@pytest.mark.parametrize('field,value', [('profile_dir', '/different-synthetic-profile'), ('account_id', 'other'), ('origin', 'https://other.invalid')])
def test_handoff_rejects_account_profile_or_origin_changes(monkeypatch, tmp_path, field, value):
    events, _ = fake_driver(monkeypatch)
    options = enroll(tmp_path)
    with IsolatedBrowser().session(**options) as session:
        with pytest.raises(ProfileNotAuthorized):
            with session.open(**{**options, field: value}):
                pytest.fail('identity mismatch must be rejected')
        with session.open(**options):
            pass
    assert events == ['launch', 'close']


def test_failed_handoff_cannot_retry_and_login_errors_close(monkeypatch, tmp_path):
    events, _ = fake_driver(monkeypatch)
    options = enroll(tmp_path)
    with IsolatedBrowser().session(**options) as session:
        with pytest.raises(RuntimeError):
            with session.open(**options):
                raise RuntimeError('synthetic failed attempt')
        with pytest.raises(BrowserUnavailable):
            with session.open(**options):
                pytest.fail('failure must consume the handoff')
    assert events == ['launch', 'close']
    with pytest.raises(RuntimeError):
        with IsolatedBrowser().session(**options):
            raise RuntimeError('synthetic cancelled login')
    assert events == ['launch', 'close', 'launch', 'close']


def test_runtime_uses_explicit_owned_browser(tmp_path):
    path = tmp_path / 'runtime.toml'
    path.write_text(BASE_CONFIG)
    browser = object()
    runtime = build_runtime(path, browser=browser)
    assert runtime.orchestrator.capabilities.require('browser') is browser


@pytest.mark.skipif(os.environ.get('KEEPFLAME_SYNTHETIC_BROWSER') != '1', reason='explicit local Chrome fixture only')
def test_session_cookie_survives_login_to_runtime_handoff(monkeypatch, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import ssl
    import subprocess
    import threading
    from playwright.sync_api import BrowserType, BrowserContext
    from keepflame.core.models import KeepResult, KeepStatus
    from keepflame.core.plugins import EntryPointKeeperRegistry

    key, cert = tmp_path / 'synthetic.key', tmp_path / 'synthetic.crt'
    subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                    '-out', str(cert), '-days', '1', '-subj', '/CN=localhost'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    observations = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            if self.path not in {'/login', '/status'}:
                self.send_response(404)
                self.end_headers()
                return
            authenticated = 'synthetic_session=local-fixture' in self.headers.get('Cookie', '')
            self.send_response(200)
            if self.path == '/login':
                self.send_header('Set-Cookie', 'synthetic_session=local-fixture; Path=/; Secure; HttpOnly; SameSite=Lax')
                body = b'<!doctype html><title>KeepFlame synthetic handoff</title>'
            else:
                body = json.dumps({'authenticated': authenticated}).encode()
                observations.append(authenticated)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    origin = 'https://127.0.0.1:' + str(server.server_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    launches = []
    original_launch = BrowserType.launch_persistent_context

    def launch(self, *args, **kwargs):
        launches.append(True)
        # Test transport only: local TLS and denied external DNS. Product options stay unchanged.
        kwargs['ignore_https_errors'] = True
        kwargs['args'].append('--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1')
        return original_launch(self, *args, **kwargs)

    def forbid_export(*_args, **_kwargs):
        pytest.fail('handoff must not read or export cookie/storage state')

    monkeypatch.setattr(BrowserType, 'launch_persistent_context', launch)
    monkeypatch.setattr(BrowserContext, 'cookies', forbid_export)
    monkeypatch.setattr(BrowserContext, 'storage_state', forbid_export)
    options = enroll(tmp_path)
    acquired = []

    class SyntheticKeeper:
        def keep(self, context):
            with context.capabilities.require('browser').open(**{**options, 'account_id': context.account_id, 'headless': True}) as page:
                acquired.append(page)
                response = page.context.request.get(origin + '/status', timeout=5000, max_redirects=0, max_retries=0)
                authenticated = response.json()['authenticated']
                response.dispose()
                return KeepResult(KeepStatus.SUCCESS if authenticated else KeepStatus.WAITING_HUMAN, 'synthetic state')

    try:
        with IsolatedBrowser().session(**options) as session:
            page = session.page
            # Route only this local fixture; other page destinations remain blocked.
            page.context.route('**/*', lambda route: route.continue_()
                               if route.request.url.startswith(origin + '/') else route.abort('blockedbyclient'))
            page.goto(origin + '/login', wait_until='domcontentloaded')
            config = tmp_path / 'runtime.toml'
            config.write_text(BASE_CONFIG.replace('account_id = "primary"', 'account_id = "synthetic"'))
            runtime = build_runtime(config, browser=session)
            runtime.orchestrator.registry = EntryPointKeeperRegistry({'joinquant': SyntheticKeeper})
            outcome = runtime.orchestrator.run_flame(runtime.config.flames[0], notify=False)
            assert outcome.record.result.status is KeepStatus.SUCCESS
            assert acquired == [page] and observations == [True] and len(launches) == 1
            assert not page.is_closed()
            with pytest.raises(BrowserUnavailable):
                with session.open(**options):
                    pytest.fail('the handoff was already consumed')
        assert page.is_closed()
        target = os.environ.get('KEEPFLAME_SESSION_ARTIFACTS')
        if target:
            Path(target).write_text(json.dumps({'same_page': True, 'native_authenticated': True, 'browser_launches': len(launches),
                                              'cookie_export_calls': 0, 'real_service_requests': 0, 'browser_closed': page.is_closed()}, indent=2) + '\n')
    finally:
        server.shutdown()
        server.server_close()
