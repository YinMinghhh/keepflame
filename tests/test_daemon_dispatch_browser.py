"""Two bounded Chrome cases for the new client -> daemon -> Keeper path.

Fresh synthetic profile, in-memory responses, no server or real website traffic.
The production pool and JoinQuant request guards stay installed throughout.
"""

from contextlib import contextmanager
import io
import json
import os
import shutil
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from keepflame.capabilities.browser import IsolatedBrowser
from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.cli import main
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN, JoinQuantKeeper
from keepflame.runtime import build_runtime
from tests.helpers import FixedClock, StaticRegistry
from tests.test_config import write_config
from tests.test_daemon_dispatch import CONFIG, NOW, FrozenDateTime, request

pytestmark = pytest.mark.skipif(os.environ.get('KEEPFLAME_SYNTHETIC_BROWSER') != '1',
                                reason='explicit isolated Chrome fixture only')


@pytest.mark.parametrize('case', ['guarded_reading_failure', 'closed_context'])
def test_client_handoff_preserves_native_session_and_recovery_gates(tmp_path, monkeypatch, capsys, case):
    from playwright.sync_api import BrowserContext
    profile = tmp_path/'synthetic-profile'
    profile.mkdir(mode=0o700)
    marker = profile/'.keepflame-profile.json'
    marker.write_text(json.dumps({'version':1,'account_id':'synthetic','purpose':'keepflame'}))
    marker.chmod(0o600)
    contexts, pages, document_auth, transport = [], [], [], []

    def no_export(*args, **kwargs):
        pytest.fail('a session must not be read or exported')
    monkeypatch.setattr(BrowserContext, 'cookies', no_export)
    monkeypatch.setattr(BrowserContext, 'storage_state', no_export)

    class SyntheticBrowser:
        @contextmanager
        def session(self, **options):
            assert options['profile_dir'] == str(profile)
            with IsolatedBrowser().session(**options) as owned:
                contexts.append(owned.page.context)
                def login_fixture(route):
                    # This is below the unchanged pool's owned-page/login gate.
                    assert route.request.url == JOINQUANT_ORIGIN+'/user/login'
                    assert route.request.method == 'GET'
                    route.fulfill(status=200, headers={'content-type':'text/html',
                        'set-cookie':'synthetic_auth=fixture-only; Path=/; HttpOnly; Secure; SameSite=Lax'},
                        body='<title>Synthetic login</title>')
                owned.page.context.route('**/*', login_fixture)
                yield owned

    pool = BrowserPool(SyntheticBrowser(), headless=True)
    begin_login = pool.begin_login
    def expose_fixture_failure(**options):
        try:
            return begin_login(**options)
        except Exception as exc:
            pytest.fail(f'synthetic login fixture failed: {type(exc).__name__}: {exc}')
    monkeypatch.setattr(pool, 'begin_login', expose_fixture_failure)
    text = CONFIG.replace('cookie_file = "/run/secrets/joinquant.cookie"', '')
    text = text.replace('[flames.settings]',
        f'[flames.settings]\nmode = "browser"\nbrowser_profile_dir = "{profile}"')
    text = text.replace('task = "reading"',
        'task = "reading"\narticle_url = "https://www.joinquant.com/view/community/detail/12345"')
    path = write_config(tmp_path, text)
    monkeypatch.setattr('keepflame.runtime.BrowserPool', lambda: pool)
    monkeypatch.setattr('keepflame.runtime.SystemClock', lambda: FixedClock(NOW))
    monkeypatch.setattr('keepflame.cli.datetime', FrozenDateTime)

    def fetch(source, *, timeout):
        req = source.request
        parsed = urlsplit(req.url)
        assert parsed.scheme == 'https' and parsed.netloc == 'www.joinquant.com'
        transport.append([req.method, parsed.path])
        assert req.method == 'GET', 'no write may reach the fixture transport'
        if parsed.path in ('/view/user/floor','/view/community/detail/12345'):
            document_auth.append('synthetic_auth=fixture-only' in (req.header_value('cookie') or ''))
            extra = ("<script>fetch('/synthetic/unknown-write',{method:'POST'}).catch(()=>{});</script>"
                     if parsed.path.startswith('/view/community/') else '')
            return 200, {'content-type':'text/html'}, ('<title>Synthetic task</title>'+extra).encode()
        assert parsed.path == '/credits/task/getTaskInfo'
        return 200, {'content-type':'application/json'}, json.dumps({
            'code':'00000','data':{'taskList':{'login':{'status':1},'post_view':{}}}}).encode()

    def page_factory(page, **kwargs):
        pages.append(page)
        assert page.context is contexts[0]
        return JoinQuantPage(page, request_fetch=fetch, **kwargs)

    class Keeper(JoinQuantKeeper):
        def keep(self, context):
            return JoinQuantBrowserKeeper(page_factory=page_factory).keep(context)

    runtime = build_runtime(path, stream=io.StringIO())
    registry = StaticRegistry(Keeper())
    runtime.orchestrator.registry = runtime.login.registry = registry
    monkeypatch.setattr('keepflame.cli.build_runtime', lambda *_: pytest.fail('client must not own a browser'))

    def client(flame, identifier=None):
        identifier = identifier or str(uuid4())
        assert main(['run-once','--via-daemon','--execute','--config',str(path),
                     '--flame',flame,'--request-id',identifier]) == 0
        payload = json.loads(capsys.readouterr().out)
        return identifier, payload

    try:
        with runtime.daemon_session():
            login = runtime.storage.request_login('joinquant','synthetic','joinquant-primary',NOW)
            runtime.poll_controls()
            assert runtime.storage.login_requests()[0]['state'] == 'waiting'
            assert runtime.storage.respond_login(login, confirm=True, now=NOW)
            runtime.poll_controls()
            assert contexts[0].pages == []
            checkin, queued = client('joinquant-primary')
            assert queued['state'] == 'requested'
            runtime.poll_controls()
            assert request(runtime, checkin)['result_status'] == 'SUCCESS'
            assert len(contexts) == len(pages) == 1 and pages[0].is_closed()
            client('joinquant-primary', checkin)
            runtime.poll_controls()
            assert len(pages) == 1
            if case == 'closed_context':
                contexts[0].close()  # Abrupt loss is synthetic; never reopen automatically.
            reading, _ = client('reading')
            runtime.poll_controls()
            state = runtime.storage.get_state('reading','synthetic')
            assert state.status.value != 'SUCCESS'
            assert state.metadata['automation_paused']
            if case == 'guarded_reading_failure':
                assert state.metadata['reason'] == 'unexpected_write'
                assert state.metadata['blocked_request']['endpoint'] == 'unknown'
                assert runtime.storage.get_action('joinquant','synthetic','reading').state == 'pending'
                assert len(pages) == 2 and all(p.is_closed() for p in pages)
                assert document_auth == [True, True]
                assert ('POST', '/synthetic/unknown-write') not in map(tuple, transport)
            else:
                assert len(pages) == 1 and document_auth == [True]
            again, _ = client('reading')
            runtime.poll_controls()
            assert request(runtime, again)['state'] == 'rejected'
            assert request(runtime, again)['reason'] == 'automation_paused'
            assert len(contexts) == 1
            assert runtime.storage.count_runs() == 2
        (tmp_path/'synthetic-evidence.json').write_text(json.dumps({
            'case':case,'browser_launches':len(contexts),'task_pages':len(pages),
            'document_cookie_present':document_auth,'transport':transport,
            'all_task_pages_closed':all(p.is_closed() for p in pages),
            'runner_lock_released':not runtime.storage.runner_active(),
            'restore_enabled':False,'cookie_export_calls':0,'real_service_requests':0,
            'reading_result':state.status.value,'reading_reason':state.metadata['reason']},indent=2)+'\n')
    finally:
        runtime.close()
        pool.close()
        shutil.rmtree(profile)
