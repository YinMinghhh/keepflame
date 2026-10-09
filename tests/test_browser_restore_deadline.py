"""Driver metadata deadlines and terminal owner state; no network by default."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from keepflame.capabilities.browser import BrowserRestoreFailed, BrowserUnavailable
from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.capabilities.browser_restore import require_bounded_driver, restore_deadline
from keepflame.runtime import build_runtime
from tests.test_config import write_config
from tests.test_runtime_control import OPTIONS


def test_deadline_reaches_protocol_and_preserves_smaller_request_timeout(monkeypatch):
    clock = [10.0]
    calls = []
    class Connection:
        def _send_message_to_server(self, object, method, params, timeout, no_reply=False):
            calls.append((object, method, params, timeout, no_reply))
            return 'sent'
    connection = Connection()
    original = connection._send_message_to_server
    driver = SimpleNamespace(_impl_obj=SimpleNamespace(_connection=connection))
    monkeypatch.setattr('keepflame.capabilities.browser_restore.time.monotonic', lambda:clock[0])
    with restore_deadline(driver, 20):
        assert connection._send_message_to_server('context','route',{},None) == 'sent'
        clock[0] += 7
        connection._send_message_to_server('page','close',{},0)
        connection._send_message_to_server('context','fetch',{},1000)
        clock[0] += 13
        with pytest.raises(TimeoutError, match='restore deadline exceeded'):
            connection._send_message_to_server('context','newPage',{},None)
    assert [row[3] for row in calls] == [20000, 13000, 1000]
    assert connection._send_message_to_server == original
    connection._send_message_to_server('context','business',{},None)
    assert calls[-1][3] is None  # adapter does not remain on idle/business calls


def test_deadline_restored_after_exception_and_unknown_transport_fails_closed():
    class Connection:
        def _send_message_to_server(self, object, method, params, timeout, no_reply=False):
            raise ValueError('synthetic transport failure')
    connection = Connection(); original = connection._send_message_to_server
    driver = SimpleNamespace(_impl_obj=SimpleNamespace(_connection=connection))
    with pytest.raises(ValueError):
        with restore_deadline(driver, 5):
            connection._send_message_to_server(None,'route',{},None)
    assert connection._send_message_to_server == original
    connection._send_message_to_server = lambda *args:None
    with pytest.raises(RuntimeError, match='unsupported browser deadline transport'):
        with restore_deadline(driver, 5):
            pytest.fail('unsupported protocol must not proceed')


@pytest.mark.parametrize('version', ['1.62.0','1.63.1','1.64.0'])
def test_unreviewed_driver_version_cannot_enable_restore(monkeypatch, version):
    monkeypatch.setattr('keepflame.capabilities.browser_restore.version', lambda _:version)
    with pytest.raises(RuntimeError, match='reviewed Playwright 1.63.0'):
        require_bounded_driver()


def test_failed_restore_latches_pool_and_stops_owner_without_dispatch(tmp_path):
    attempts=[]
    class FailedBrowser:
        @contextmanager
        def session(self, **options):
            attempts.append(options)
            raise BrowserRestoreFailed('browser restore startup failed')
            yield  # contextmanager contract; unreachable
    pool = BrowserPool(FailedBrowser())
    stream=io.StringIO()
    runtime = build_runtime(write_config(tmp_path), stream=stream)
    runtime.owned_browser.close()
    runtime.owned_browser=pool
    controls=[]
    runtime.login.tick=lambda:controls.append('login')
    with runtime.daemon_session():
        with pytest.raises(BrowserRestoreFailed):
            with pool.open(**OPTIONS):
                pytest.fail('failed startup must never yield')
        assert pool.closed and pool.close_reason == 'restore_startup_failed'
        runtime.poll_controls()
        assert runtime.service.stop_event.is_set() and not controls
        with pytest.raises(BrowserUnavailable):
            with pool.open(**OPTIONS):
                pytest.fail('must not automatically reopen')
        assert len(attempts)==1
    stopped=[json.loads(line) for line in stream.getvalue().splitlines()
             if json.loads(line)['event']=='daemon_owner_stopped']
    assert stopped[0]['close_reason']=='restore_startup_failed'
    assert runtime.storage.count_runs()==0


def test_restore_barrier_startup_failure_also_latches_pool():
    class FailedBarrier:
        restore_session=True
        @contextmanager
        def session(self, **options):
            raise OSError('synthetic barrier bind failure')
            yield
    pool=BrowserPool(FailedBarrier())
    with pytest.raises(OSError):
        with pool.open(**OPTIONS):
            pytest.fail('must not bypass a failed barrier')
    assert pool.closed and pool.close_reason=='restore_startup_failed'


@pytest.mark.skipif(os.environ.get('KEEPFLAME_SESSION_RESTORE') != '1',
                    reason='explicit local Chrome cleanup deadline fixture only')
def test_cleanup_deadline_reaps_owned_driver_before_releasing_barrier(tmp_path):
    artifact=tmp_path/'cleanup-evidence.json'
    subprocess.run([sys.executable, str(Path(__file__).with_name('session_restore_probe.py')),
                    '--cleanup-fault','--output',str(artifact)], check=True, timeout=45)
    result=json.loads(artifact.read_text())
    assert result['result']=='bounded_cleanup_failure'
    assert result['driver_closed_before_barrier_release']
    assert result['barrier_reserved_during_shutdown']
    assert result['temporary_profile_deleted'] and result['cookie_export_calls']==0
