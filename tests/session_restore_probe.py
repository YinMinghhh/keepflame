"""Independent processes, fake cookies, own local TLS server. Never user auth.

The route.fetch rewrite is a test-only transport adapter from the production
HTTPS/443 allowlist to the fixture's ephemeral TLS port. Startup restored tabs
use the real fixture port and have no route installed during the observation.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

CANDIDATE = Path(__file__).resolve().parents[1]
os.umask(0o077)
sys.path.insert(0, str(CANDIDATE / 'src'))


def child(stage, profile, origin):
    from playwright.sync_api import BrowserContext, BrowserType, Route, sync_playwright
    from keepflame.capabilities.browser import IsolatedBrowser
    from keepflame.capabilities.browser_pool import BrowserPool

    def forbidden(*_args, **_kwargs):
        raise AssertionError('cookie/storage export forbidden')
    BrowserContext.cookies = BrowserContext.storage_state = forbidden
    result = {'stage': stage, 'pid': os.getpid(), 'launches': 0, 'cookie_export_calls': 0}
    if stage in {'seed', 'unprotected_control'}:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(str(profile), channel='chrome', headless=True,
                chromium_sandbox=True, timeout=20000, accept_downloads=False, service_workers='block',
                ignore_https_errors=True, args=['--restore-last-session', '--disable-background-networking',
                    '--disable-component-update', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                    '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1'])
            try:
                # Intentionally NO application routes: a bounded negative control
                # on our own synthetic fixture, including restored startup traffic.
                time.sleep(1)
                page = context.new_page()
                if stage == 'seed':
                    page.goto(origin + '/seed', timeout=5000)
                response = context.request.get(origin + '/status', timeout=5000, max_redirects=0, max_retries=0)
                result['auth'] = response.json(); response.dispose()
                page.goto(origin + '/restorable', timeout=5000)
                page.wait_for_timeout(300)
                result['browser_version'] = context.browser.version
                result['launches'] = 1
            finally:
                context.close()
    else:
        launch_original = BrowserType.launch_persistent_context
        fetch_original = Route.fetch
        prelaunch_options = []
        contexts = []

        def launch(self, *args, **kwargs):
            assert '--restore-last-session' in kwargs['args']
            assert '--proxy-bypass-list=<-loopback>' in kwargs['args']
            assert not any('password-store' in a or 'keychain' in a for a in kwargs['args'])
            assert 'proxy' not in kwargs and 'ignore_default_args' not in kwargs
            kwargs['ignore_https_errors'] = True  # only the fixture TLS certificate
            prelaunch_options.extend(a.split('=')[0] for a in kwargs['args'])
            context = launch_original(self, *args, **kwargs)
            contexts.append(context)
            result['launches'] += 1
            result['browser_version'] = context.browser.version
            # Enlarge the exact startup gap. Production has NOT installed a route.
            time.sleep(1)
            result['startup_page_count'] = len(context.pages)
            for scheme in ('https', 'http'):
                page = context.new_page()
                try:
                    page.goto(origin.replace('https', scheme) + '/raw-startup-probe', timeout=1500)
                    result[scheme + '_raw_navigation'] = 'unexpected_success'
                except Exception as exc:
                    result[scheme + '_raw_navigation'] = ('proxy_blocked' if 'ERR_PROXY_CONNECTION_FAILED' in str(exc)
                                                        else type(exc).__name__)
            return context

        def fetch(self, **kwargs):
            parsed = urlsplit(self.request.url)
            assert parsed.scheme == 'https' and parsed.netloc == '127.0.0.1'
            # Keep the product's method/redirect/retry policy, replace only port.
            return fetch_original(self, url=origin + parsed.path, **kwargs)

        BrowserType.launch_persistent_context = launch
        Route.fetch = fetch
        options = dict(profile_dir=str(profile), account_id='synthetic', origin='https://127.0.0.1')
        pool = BrowserPool(IsolatedBrowser(restore_session=True), headless=True)
        try:
            try:
                with pool.open(**options) as page:
                    context = page.context
                    assert all(p is page for p in context.pages)
                    page.goto('https://127.0.0.1/status', timeout=5000)
                    result['auth'] = json.loads(page.locator('body').inner_text(timeout=2000))
                    raise RuntimeError('synthetic task failure')
            except RuntimeError as exc:
                assert str(exc) == 'synthetic task failure'
            result['task_page_closed'] = page.is_closed()
            result['idle_pages'] = len(context.pages)
            time.sleep(1)
            # One new task page in the SAME context after ordinary failure.
            with pool.open(**options) as page2:
                assert page2.context is context
                response = context.request.get(origin + '/status', timeout=5000, max_redirects=0, max_retries=0)
                result['native_after_failure'] = response.json(); response.dispose()
                result['same_context_after_failure'] = True
            result['idle_pages_after_second_task'] = len(context.pages)
            result['prelaunch_flags'] = prelaunch_options
            assert result['auth'] == result['native_after_failure'] == {'session': True, 'persistent': True}
            assert result['https_raw_navigation'] == result['http_raw_navigation'] == 'proxy_blocked'
        finally:
            pool.close()
        result['owned_context_closed'] = not contexts[0].browser.is_connected()
    print(json.dumps(result, sort_keys=True), flush=True)


def cleanup_fault(target):
    """Make public close wait on a real never-resolving browser operation."""
    from playwright.sync_api import BrowserType, BrowserContext
    from keepflame.capabilities.browser import BrowserRestoreFailed, IsolatedBrowser
    from keepflame.capabilities.browser_pool import BrowserPool
    import keepflame.capabilities.browser_restore as restore
    contexts=[]; audit={}
    original_launch=BrowserType.launch_persistent_context
    original_barrier=restore.restore_barrier
    def launch(self,*args,**kwargs):
        context=original_launch(self,*args,**kwargs); contexts.append(context)
        return context
    def stall_close(context, **kwargs):
        # Runs through the real protocol; it has no public timeout parameter.
        context.new_page().evaluate('() => new Promise(() => {})')
        raise AssertionError('never-resolving close operation unexpectedly returned')
    @contextmanager
    def barrier():
        with original_barrier() as flags:
            port=int(next(a.rsplit(':',1)[-1] for a in flags if a.startswith('--proxy-server=')))
            try:
                yield flags
            finally:
                # A failed public close can leave Browser.is_connected() stale.
                # Observe the actually owned driver process after stop() joined it.
                process=contexts[0]._impl_obj._connection._transport._proc
                audit['driver_exit_code_before_barrier_release']=process.returncode
                audit['driver_closed_before_barrier_release']=process.returncode is not None
                with socket.socket() as probe:
                    try:
                        probe.bind(('127.0.0.1',port))
                        raise AssertionError('barrier reservation released too early')
                    except OSError:
                        audit['barrier_reserved_during_shutdown']=True
    def forbidden(*args,**kwargs):
        raise AssertionError('cookie/storage export forbidden')
    BrowserType.launch_persistent_context=launch
    BrowserContext.close=stall_close
    BrowserContext.cookies=BrowserContext.storage_state=forbidden
    restore.restore_barrier=barrier
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='keepflame-cleanup-deadline-', dir='/tmp') as folder:
        profile=Path(folder).resolve()/'profile'; profile.mkdir(mode=0o700)
        marker=profile/'.keepflame-profile.json'
        marker.write_text(json.dumps({'version':1,'account_id':'synthetic','purpose':'keepflame'}));marker.chmod(0o600)
        pool=BrowserPool(IsolatedBrowser(restore_session=True),headless=True)
        with pool.open(profile_dir=str(profile),account_id='synthetic',origin='https://127.0.0.1'):
            pass
        try:
            pool.close()
            raise AssertionError('cleanup stall must report failure')
        except BrowserRestoreFailed as exc:
            assert str(exc)=='browser restore cleanup failed'
            audit['result']='bounded_cleanup_failure'
    audit.update(elapsed_seconds=round(time.monotonic()-started,3), real_profile_opened=False,
                 real_service_requests=0,cookie_export_calls=0, temporary_profile_deleted=not profile.exists())
    rows=subprocess.check_output(['/bin/ps','-axo','args'],text=True).splitlines()
    audit['owned_profile_processes_remaining']=sum('--user-data-dir='+str(profile) in row for row in rows)
    target.write_text(json.dumps(audit,indent=2)+'\n')
    assert audit['elapsed_seconds'] < 40 and audit['driver_closed_before_barrier_release']
    assert audit['owned_profile_processes_remaining']==0
    print(json.dumps(audit))


def parent(target):
    events = []
    current = {'stage': 'starting'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            path = urlsplit(self.path).path
            raw = self.headers.get('Cookie', '')  # synthetic server memory only
            auth = {'session': 'fixture_session=synthetic_only' in raw,
                    'persistent': 'fixture_persistent=synthetic_only' in raw}
            events.append({'stage': current['stage'], 'method': 'GET', 'path': path, **auth})
            self.send_response(200)
            if path == '/seed':
                self.send_header('Set-Cookie', 'fixture_session=synthetic_only; Path=/; Secure; HttpOnly; SameSite=Lax')
                self.send_header('Set-Cookie', 'fixture_persistent=synthetic_only; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=3600')
            if path == '/status':
                body = json.dumps(auth).encode(); kind = 'application/json'
            elif path == '/restorable':
                body = b"<!doctype html><title>Synthetic only</title><script>fetch('/startup-post',{method:'POST'}).catch(()=>{});navigator.sendBeacon('/startup-beacon','synthetic');</script>"
                kind = 'text/html'
            else:
                body = b'<!doctype html><title>Synthetic only</title>'; kind = 'text/html'
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            events.append({'stage': current['stage'], 'method': 'POST', 'path': urlsplit(self.path).path})
            self.send_response(204); self.end_headers()

    with tempfile.TemporaryDirectory(prefix='keepflame-preserve-fixture-', dir='/tmp') as temp:
        root = Path(temp).resolve()
        key, cert = root/'fixture.key', root/'fixture.crt'
        subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes',
            '-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost'],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        profile = root/'profile'; profile.mkdir(mode=0o700)
        marker = profile/'.keepflame-profile.json'
        marker.write_text(json.dumps({'version':1,'account_id':'synthetic','purpose':'keepflame'})); marker.chmod(0o600)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        origin = 'https://127.0.0.1:' + str(server.server_port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        results = []
        try:
            for stage in ('seed', 'unprotected_control', 'protected_restore', 'protected_second_process'):
                current['stage'] = stage
                command = [sys.executable, str(Path(__file__).resolve()), stage, str(profile), origin]
                started = time.monotonic()
                proc = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
                try:
                    stdout, stderr = proc.communicate(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL); proc.communicate()
                    raise AssertionError('bounded synthetic child timeout; only its owned process group stopped')
                if proc.returncode:
                    raise AssertionError({'stage':stage, 'returncode':proc.returncode, 'stderr':stderr[-3500:]})
                item = json.loads(stdout); item['seconds'] = round(time.monotonic() - started, 3)
                results.append(item)
            protected = [e for e in events if e['stage'].startswith('protected')]
            assert all(e['method'] == 'GET' and e['path'] == '/status' for e in protected), protected
            assert len(protected) == 4
            control = [e for e in events if e['stage'] == 'unprotected_control']
            assert control[0]['path'] == '/restorable', control
            assert any(e['method'] == 'POST' for e in control)
            assert len({r['pid'] for r in results}) == 4
        finally:
            server.shutdown(); server.server_close()
    output = {'processes':results, 'server_events':events, 'real_profile_opened':False,
              'real_service_requests':0, 'cookie_export_calls':0, 'max_concurrent_browsers':1,
              'temporary_profiles_cleaned':True, 'encryption_provider_changed':False,
              'system_network_settings_changed':False, 'protected_server_requests':protected}
    target.write_text(json.dumps(output,indent=2)+'\n')
    print(json.dumps({'result':str(target), 'processes':results, 'protected_server_requests':protected},indent=2))


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] in {'seed','unprotected_control','protected_restore','protected_second_process'}:
        child(sys.argv[1], Path(sys.argv[2]), sys.argv[3])
    else:
        import argparse
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--output', type=Path, required=True)
        parser.add_argument('--cleanup-fault', action='store_true')
        args=parser.parse_args()
        (cleanup_fault if args.cleanup_fault else parent)(args.output)
