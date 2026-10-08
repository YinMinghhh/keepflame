"""Local daemon handoff, using only new databases and fake browser contexts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import signal
import sqlite3
from uuid import uuid4

import pytest

from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.cli import main
from keepflame.core.config import load_config
from keepflame.core.models import KeepResult, KeepStatus
from keepflame.runtime import build_runtime
from keepflame.storage import SQLiteStorage
from keepflame.storage.dispatch import DispatchError
from keepflame.storage.sqlite import StorageError
from tests.helpers import FixedClock, StaticRegistry
from tests.test_config import BASE_CONFIG, write_config
from tests.test_runtime_control import Browser, OPTIONS

NOW = datetime(2026, 10, 8, 2, tzinfo=timezone.utc)
CONFIG = BASE_CONFIG.replace('account_id = "primary"', 'account_id = "synthetic"')
CONFIG += '\n[[flames]]' + CONFIG.split('[[flames]]', 1)[1].replace(
    'id = "joinquant-primary"', 'id = "reading"').replace(
    'cookie_file = "/run/secrets/joinquant.cookie"', 'task = "reading"')


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def setup(tmp_path, monkeypatch, keeper_factory=None):
    clock, browser = FixedClock(NOW), Browser()
    pool = BrowserPool(browser)
    path = write_config(tmp_path, CONFIG)
    stream = io.StringIO()
    monkeypatch.setattr('keepflame.runtime.BrowserPool', lambda: pool)
    monkeypatch.setattr('keepflame.runtime.SystemClock', lambda: clock)
    monkeypatch.setattr('keepflame.cli.datetime', FrozenDateTime)
    runtime = build_runtime(path, stream=stream)
    visits = []

    class Keeper:
        def keep(self, context):
            with context.capabilities.require('browser').open(**OPTIONS) as page:
                visits.append(page)
                return KeepResult(KeepStatus.SUCCESS, 'synthetic completed')

    runtime.orchestrator.registry = StaticRegistry(Keeper() if keeper_factory is None else keeper_factory(visits))
    return runtime, browser, pool, visits, clock, stream


def submit(runtime, index=0, identifier=None):
    return runtime.storage.submit_run(identifier or str(uuid4()), runtime.config,
                                      runtime.config.flames[index], runtime.orchestrator.clock.now())


def request(runtime, identifier):
    return next(r for r in runtime.storage.dispatch_status()['requests'] if r['id'] == identifier)


def test_cli_handoff_two_tasks_reuses_context_without_client_runtime(tmp_path, monkeypatch, capsys):
    runtime, browser, pool, visits, clock, stream = setup(tmp_path, monkeypatch)
    def no_runtime(*args, **kwargs):
        pytest.fail('a handoff client must not construct a runtime or browser')
    monkeypatch.setattr('keepflame.cli.build_runtime', no_runtime)
    with runtime.daemon_session():
        for flame in runtime.config.flames:
            identifier = str(uuid4())
            argv = ['run-once', '--via-daemon', '--execute', '--config', str(runtime.config.source_path),
                    '--flame', flame.id, '--request-id', identifier]
            assert main(argv) == 0
            assert json.loads(capsys.readouterr().out)['state'] == 'requested'
            assert len(visits) == (0 if flame.id == 'joinquant-primary' else 1)
            runtime.poll_controls()
            row = request(runtime, identifier)
            assert row['state'] == 'completed' and row['result_status'] == 'SUCCESS' and row['run_id']
            assert main(argv) == 0
            assert json.loads(capsys.readouterr().out)['execution_completed']
            runtime.poll_controls()
        assert len(visits) == 2 and visits[0] is not visits[1]
        assert all(p.closed for p in visits)
        assert visits[0].context is visits[1].context
        assert len(browser.launches) == 1 and not browser.launches[0].closed
        assert runtime.storage.count_runs() == 2
        # A scheduler tick after handoff cannot repeat these completed daily tasks.
        assert runtime.service.run_due(now=NOW)[0] == []
        assert len(visits) == 2
    assert browser.launches[0].closed and not runtime.storage.runner_active()
    assert not runtime.storage.dispatch_status()['owner_record']['active']
    assert 'daemon_owner_stopped' in stream.getvalue()
    runtime.close()  # Owner shutdown is idempotent.


def test_real_client_process_exits_without_owning_the_context(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    import subprocess
    import sys
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    # The subprocess may touch only this fixture's database. Its own audit also
    # denies sockets, browser launches and all real account files.
    bootstrap = tmp_path/'dispatch_client_fixture.py'
    bootstrap.write_text('''import os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
def audit(event, args):
    if event in {'socket.connect','socket.getaddrinfo','socket.sendto','subprocess.Popen'}:
        raise AssertionError('synthetic CLI client attempted network or subprocess')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(part in str(path) for part in ('/Library/Application Support/', '/Library/Keychains/', '/Library/Cookies/')):
            raise AssertionError('synthetic CLI client attempted real account file access')
        mode, flags = args[1:3]
        writing = (isinstance(mode,str) and any(v in mode for v in 'wa+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
        if writing and not path.is_relative_to(root):
            raise AssertionError('synthetic CLI client attempted an outside write')
sys.addaudithook(audit)
from datetime import datetime, timezone
class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 8, 2, tzinfo=timezone.utc)
import keepflame.cli as cli
cli.datetime = Clock
raise SystemExit(cli.main())
''')
    with runtime.daemon_session():
        with pool.open(**OPTIONS) as warm:
            original = warm.context
        identifier = str(uuid4())
        env = {**os.environ, 'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src'),
               'PYTHONDONTWRITEBYTECODE':'1'}
        result = subprocess.run([sys.executable,str(bootstrap),'run-once','--via-daemon','--execute',
            '--config',str(runtime.config.source_path),'--flame','joinquant-primary','--request-id',identifier],
            capture_output=True,text=True,timeout=15,env=env,check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['state'] == 'requested'
        assert not original.closed and len(browser.launches) == 1
        runtime.poll_controls()
        assert request(runtime, identifier)['state'] == 'completed'
        assert visits[0].context is original and not original.closed
    assert original.closed


def test_idle_and_borrower_close_do_not_close_owner_context(tmp_path, monkeypatch):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    with runtime.daemon_session():
        submit(runtime)
        runtime.poll_controls()
        borrowed = build_runtime(runtime.config.source_path, browser=pool, stream=io.StringIO())
        borrowed.close()
        for _ in range(4):
            clock.current += timedelta(minutes=20)
            runtime.poll_controls()
        assert len(browser.launches) == 1 and not browser.launches[0].closed
        submit(runtime, 1)
        runtime.poll_controls()
        assert len(visits) == 2 and visits[0].context is visits[1].context


def test_scheduler_leaves_queued_flame_to_its_single_dispatch(tmp_path, monkeypatch):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    with runtime.daemon_session():
        first, second = submit(runtime), submit(runtime, 1)
        assert runtime.service.run_due(now=NOW)[0] == []
        assert request(runtime, first['id'])['state'] == 'completed'
        assert request(runtime, second['id'])['state'] == 'requested'
        assert len(visits) == 1
        assert runtime.service.run_due(now=NOW)[0] == []
        assert request(runtime, second['id'])['state'] == 'completed'
        assert len(visits) == 2


@pytest.mark.parametrize('same_id', [True, False])
def test_concurrent_clients_have_one_queue_entry(tmp_path, monkeypatch, same_id):
    from concurrent.futures import ThreadPoolExecutor
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    identifier = str(uuid4())
    def client(index):
        storage = SQLiteStorage(runtime.storage.path, existing_only=True)
        try:
            return storage.submit_run(identifier if same_id or index == 0 else str(uuid4()),
                                      runtime.config, runtime.config.flames[0], NOW)['id']
        except DispatchError:
            return 'duplicate_rejected'
    with runtime.daemon_session():
        with ThreadPoolExecutor(2) as clients:
            results = list(clients.map(client, (0,1)))
        assert len(runtime.storage.dispatch_status()['requests']) == 1
        assert results == [identifier,identifier] if same_id else results.count('duplicate_rejected') == 1
        runtime.poll_controls()
        assert len(visits) == 1


def test_second_daemon_cannot_retire_the_active_owners_queue(tmp_path, monkeypatch):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    other = build_runtime(runtime.config.source_path, browser=object(), stream=io.StringIO())
    try:
        with runtime.daemon_session():
            row = submit(runtime)
            with pytest.raises(StorageError, match='another KeepFlame runner'):
                with other.daemon_session():
                    pytest.fail('a second daemon must never enter')
            assert request(runtime, row['id'])['state'] == 'requested'
            runtime.poll_controls()
            assert len(visits) == 1
    finally:
        other.close()


@pytest.mark.parametrize('case', ['dry_run','absent_database','absent_owner','stopped_owner','stale_owner','wrong_database','invalid_uuid','disabled'])
def test_client_refuses_without_fallback(tmp_path, monkeypatch, capsys, case):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    path = runtime.config.source_path
    if case == 'stopped_owner':
        with runtime.daemon_session():
            pass
    elif case == 'stale_owner':
        with runtime.storage.execution_lock():
            runtime.storage.start_dispatch(str(uuid4()), runtime.config, NOW)
    elif case in ('absent_database', 'wrong_database'):
        path.write_text(CONFIG.replace('state/keepflame.sqlite3', 'never-created/db.sqlite3'))
    elif case == 'disabled':
        path.write_text(CONFIG.replace('enabled = true', 'enabled = false'))
    monkeypatch.setattr('keepflame.cli.build_runtime', lambda *_: pytest.fail('client fallback'))
    argv = ['run-once','--via-daemon','--config',str(path),'--flame','joinquant-primary',
            '--request-id', 'invalid' if case == 'invalid_uuid' else str(uuid4())]
    if case != 'dry_run':
        argv.append('--execute')
    assert main(argv) == (0 if case == 'dry_run' else 1)
    capsys.readouterr()
    assert not (tmp_path/'never-created').exists() and not browser.launches
    assert runtime.storage.dispatch_status()['requests'] == []
    runtime.close()


@pytest.mark.parametrize('argv', [['--via-daemon'], ['--request-id','unused'], ['--via-daemon','--flame','reading']])
def test_incomplete_cli_scope_has_no_side_effects(tmp_path, monkeypatch, capsys, argv):
    path = write_config(tmp_path, CONFIG)
    monkeypatch.setattr('keepflame.cli.build_runtime', lambda *_: pytest.fail('scope must fail first'))
    assert main(['run-once','--execute','--config',str(path), *argv]) == 1
    capsys.readouterr()
    assert not (tmp_path/'state').exists()


def test_duplicates_capacity_and_expiry_are_bounded(tmp_path, monkeypatch):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    with runtime.daemon_session():
        first = submit(runtime)
        assert submit(runtime, identifier=first['id']) == first
        with pytest.raises(DispatchError, match='unfinished'):
            submit(runtime)
        with pytest.raises(DispatchError, match='another target'):
            submit(runtime, 1, first['id'])
        for index in range(15):
            # Storage capacity is independent of any platform or actual Flame count.
            flame = replace(runtime.config.flames[0], id=f'synthetic-{index}')
            runtime.storage.submit_run(str(uuid4()), runtime.config, flame, NOW)
        with pytest.raises(DispatchError, match='full'):
            submit(runtime, 1)
        clock.current += timedelta(minutes=5)
        runtime.poll_controls()
        assert all(r['state'] == 'expired' for r in runtime.storage.dispatch_status()['requests'])
        assert not visits and not browser.launches
        assert submit(runtime, identifier=first['id'])['state'] == 'expired'


@pytest.mark.parametrize('case', ['pause','login','config_edit','reload','disabled','outside_window','cooldown','success_today'])
def test_dispatch_respects_existing_gates(tmp_path, monkeypatch, case):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    flame = runtime.config.flames[0]
    with runtime.daemon_session():
        if case in ('cooldown','success_today'):
            class ResultKeeper:
                def keep(self, context):
                    return KeepResult(KeepStatus.FAILED if case == 'cooldown' else KeepStatus.SUCCESS,
                                      'synthetic prior status', next_action_at=NOW+timedelta(hours=4))
            runtime.orchestrator.registry = StaticRegistry(ResultKeeper())
            runtime.orchestrator.run_flame(flame, notify=False)
        row = submit(runtime)
        if case == 'pause':
            runtime.storage.pause(flame.keeper, flame.account_id, '*', 'synthetic pause', NOW)
        elif case == 'login':
            runtime.storage.request_login(flame.keeper, flame.account_id, flame.id, NOW)
            monkeypatch.setattr(runtime.login, 'tick', lambda: None)
        elif case in ('config_edit','reload','disabled'):
            runtime.config.source_path.write_text(CONFIG.replace('enabled = true', 'enabled = false') if case == 'disabled' else CONFIG+'\n# edit\n')
            if case in ('reload','disabled'):
                runtime.storage.request_reload(NOW)
        elif case == 'outside_window':
            clock.current = NOW.replace(hour=23)  # Outside next day's local window.
            # Queue at that time so this checks schedule rather than queue expiry.
            with runtime.storage._connect() as db:
                db.execute('UPDATE run_requests SET expires_at=?', ((clock.current+timedelta(minutes=5)).isoformat(),))
        runtime.poll_controls()
        observed = request(runtime, row['id'])
        assert observed['state'] == 'rejected'
        assert observed['reason'] in {'automation_paused','configuration_changed','flame_disabled_or_changed','not_due'}
        assert not visits and not browser.launches


def test_changed_config_requires_reload_and_does_not_retarget_queued_run(tmp_path, monkeypatch):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    with runtime.daemon_session():
        old = submit(runtime)
        runtime.config.source_path.write_text(CONFIG.replace('account_id = "synthetic"','account_id = "other"'))
        candidate = load_config(runtime.config.source_path)
        with pytest.raises(DispatchError, match='reload'):
            runtime.storage.submit_run(str(uuid4()), candidate, candidate.flames[1], NOW)
        runtime.storage.request_reload(NOW)
        runtime.poll_controls()
        assert request(runtime, old['id'])['state'] == 'rejected'
        assert not visits


def test_keeper_exception_closes_page_keeps_context_and_preserves_pending(tmp_path, monkeypatch):
    def factory(visits):
        class Keeper:
            def keep(self, context):
                with context.capabilities.require('browser').open(**OPTIONS) as page:
                    visits.append(page)
                    if context.flame_id == 'joinquant-primary':
                        assert context.storage.reserve_action('joinquant','synthetic','checkin','2026-10-08',NOW)
                        raise RuntimeError('synthetic private exception')
                    return KeepResult(KeepStatus.SUCCESS, 'synthetic reading')
        return Keeper()
    runtime, browser, pool, visits, clock, stream = setup(tmp_path, monkeypatch, factory)
    with runtime.daemon_session():
        row = submit(runtime)
        runtime.poll_controls()
        assert request(runtime, row['id'])['result_status'] == 'FAILED'
        assert visits[0].closed and not visits[0].context.closed
        submit(runtime, 1)
        runtime.poll_controls()
        assert len(browser.launches) == 1 and visits[1].context is visits[0].context
        assert runtime.storage.get_action('joinquant','synthetic','checkin').state == 'pending'
        assert 'synthetic private exception' not in stream.getvalue()


@pytest.mark.parametrize('failure', ['before_keeper','after_result_record'])
def test_dispatch_control_failure_stops_owner_and_does_not_replay(tmp_path, monkeypatch, failure):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    def fail(*args, **kwargs): raise StorageError('synthetic storage unavailable')
    with pytest.raises(StorageError):
        with runtime.daemon_session():
            row = submit(runtime)
            runtime.storage.reserve_action('joinquant','synthetic','reading','2026-10-08',NOW)
            if failure == 'before_keeper':
                monkeypatch.setattr(runtime.orchestrator, 'run_flame', fail)
            else:
                monkeypatch.setattr(runtime.storage, 'finish_run_request', fail)
            runtime.poll_controls()
    assert request(runtime, row['id'])['state'] == 'interrupted'
    assert runtime.storage.is_paused('joinquant','synthetic','joinquant-primary')
    assert runtime.storage.get_action('joinquant','synthetic','reading').state == 'pending'
    assert not runtime.storage.runner_active() and all(c.closed for c in browser.launches)


def test_restart_retires_old_generation_without_browser_restore_or_action_reset(tmp_path, monkeypatch):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    old_owner = str(uuid4())
    # Synthetic crash boundary: persist the exact durable states, release the
    # OS lock without orderly owner cleanup; no real process is killed.
    with runtime.storage.execution_lock(), runtime.storage.daemon_owner_lock(old_owner):
        runtime.storage.start_dispatch(old_owner, runtime.config, NOW)
        running = submit(runtime)
        runtime.storage.claim_run(old_owner, NOW)
        queued = submit(runtime, 1)
        runtime.storage.reserve_action('joinquant','synthetic','reading','2026-10-08',NOW)
    clock.current += timedelta(days=1)
    with runtime.daemon_session():
        assert runtime.dispatch_owner != old_owner
        assert request(runtime, running['id'])['state'] == 'interrupted'
        assert request(runtime, queued['id'])['state'] == 'cancelled'
        assert runtime.storage.is_paused('joinquant','synthetic','joinquant-primary')
        assert submit(runtime, identifier=running['id'])['state'] == 'interrupted'
        runtime.poll_controls()
        assert not browser.launches and not visits
        assert runtime.storage.get_action('joinquant','synthetic','reading').state == 'pending'


def test_stop_cancels_queued_work_and_closes_browser_before_unlock(tmp_path, monkeypatch):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    original_close = pool.close
    checks = []
    def close():
        status = runtime.storage.dispatch_status()
        checks.append((status['runner_lock_held'], status['daemon_owner_lock_held']))
        original_close()
    monkeypatch.setattr(pool, 'close', close)
    with runtime.daemon_session():
        row = submit(runtime)
        runtime.service.stop_event.set()
        runtime.poll_controls()
        assert request(runtime, row['id'])['state'] == 'requested'
    assert request(runtime, row['id'])['state'] == 'cancelled'
    assert checks == [(True, True)] and not runtime.storage.runner_active() and not visits
    assert not runtime.storage.dispatch_status()['daemon_owner_lock_held']
    assert not list(runtime.storage.path.parent.glob('*.daemon-*.lock'))


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
def test_daemon_cli_signal_uses_orderly_shutdown_without_process_signal(tmp_path, monkeypatch, signum):
    runtime, browser, pool, visits, clock, _ = setup(tmp_path, monkeypatch)
    from keepflame.cli import _run_daemon
    handlers = {}
    def register(number, handler):
        prior = handlers.get(number, signal.SIG_DFL)
        handlers[number] = handler
        return prior
    monkeypatch.setattr(signal, 'signal', register)
    def loop():
        handlers[signum](signum, None)
        assert runtime.service.stop_event.is_set()
    monkeypatch.setattr(runtime.service, 'run_forever', loop)
    with runtime.daemon_session():
        row = submit(runtime)
        assert _run_daemon(runtime) == 0
    assert request(runtime, row['id'])['state'] == 'cancelled'
    assert set(handlers.values()) == {signal.SIG_DFL}


def test_schema_three_migration_preserves_pending_pause_and_history(tmp_path):
    from tests.test_storage import record_at
    path = tmp_path/'old.sqlite3'
    old = SQLiteStorage(path)
    old.reserve_action('joinquant','synthetic','reading','2026-10-08',NOW)
    old.pause('joinquant','synthetic','reading','unconfirmed',NOW)
    run, state = record_at('existing-run', 'synthetic history', NOW)
    old.record(run, state)
    with sqlite3.connect(path) as db:
        db.executescript('DROP TABLE daemon_owner; DROP TABLE run_requests; UPDATE schema_meta SET version=3;')
    with pytest.raises(StorageError):
        SQLiteStorage(path, existing_only=True)  # Submission cannot migrate.
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version FROM schema_meta').fetchone()[0] == 3
    upgraded = SQLiteStorage(path)
    assert upgraded.get_action('joinquant','synthetic','reading').state == 'pending'
    assert upgraded.is_paused('joinquant','synthetic','reading')
    assert upgraded.dispatch_status()['requests'] == []
    assert upgraded.count_runs() == 1
    assert upgraded.get_state(state.flame_id,state.account_id) == state
