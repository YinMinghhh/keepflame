"""Regression for P2: a standalone runner cannot revive a crashed daemon owner."""

import json
import os
from pathlib import Path
import select
import subprocess
import sys
from uuid import uuid4

import pytest

import keepflame
from keepflame.storage import SQLiteStorage
from keepflame.storage.dispatch import DispatchError
from tests.test_config import write_config
from tests.test_daemon_dispatch import CONFIG, NOW, setup, submit


def test_crashed_daemon_then_real_standalone_rejects_third_process_client(tmp_path):
    path = write_config(tmp_path, CONFIG)
    fixture = Path(__file__).with_name('daemon_owner_process.py')
    env = {**os.environ,'PYTHONPATH':str(Path(keepflame.__file__).resolve().parent.parent),
           'PYTHONDONTWRITEBYTECODE':'1'}
    children = []
    def child(mode, *args):
        process = subprocess.Popen([sys.executable,str(fixture),mode,str(path),*args],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        children.append(process)
        return process
    def ready(process):
        assert select.select([process.stdout], [], [], 10)[0], 'child readiness timed out'
        value = process.stdout.readline()
        assert value, process.stderr.read()
        return json.loads(value)
    def release(process):
        process.stdin.write('GO\n')
        process.stdin.flush()
        return process.communicate(timeout=15)
    try:
        daemon = child('crash_daemon')
        old_owner = ready(daemon)['owner_id']
        _, errors = release(daemon)
        assert daemon.returncode == 76, errors
        storage = SQLiteStorage(path.parent/'state/keepflame.sqlite3',existing_only=True)
        assert storage.dispatch_status()['owner_record']['active'] == 1
        standalone = child('standalone')
        assert ready(standalone)['kind'] == 'standalone'
        assert storage.runner_active()  # The misleading witness from the P2.
        client = child('client',str(uuid4()))
        stdout, stderr = client.communicate(timeout=15)
        during = storage.dispatch_status()
        result, standalone_errors = release(standalone)
        assert standalone.returncode == 0, standalone_errors
        after = storage.dispatch_status()
        observation = {'crashed_daemon_exit':daemon.returncode,'old_owner_id':old_owner,
            'runner_lock_held_during_client':during['runner_lock_held'],
            'daemon_owner_lock_held_during_client':during.get('daemon_owner_lock_held'),
            'client_exit':client.returncode,'client_stdout':json.loads(stdout) if stdout else None,
            'client_stderr':json.loads(stderr) if stderr else None,'queue_count':len(after['requests']),
            'standalone_result':json.loads(result),'all_child_exits':[p.returncode for p in children]}
        (tmp_path/'owner-liveness-evidence.json').write_text(json.dumps(observation,indent=2)+'\n')
        assert client.returncode == 1, 'stale owner was accepted while only ordinary run-once held the runner lock'
        assert not stdout and observation['client_stderr']['error_type'] == 'DispatchError'
        assert after['requests'] == []
        assert during['daemon_owner_lock_held'] is False
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()  # Only handles created by this synthetic test.
                process.communicate(timeout=5)


def test_owner_proof_is_bound_to_generation_not_any_daemon_lock(tmp_path, monkeypatch):
    runtime, browser, pool, visits, clock, stream = setup(tmp_path, monkeypatch)
    stale, other = str(uuid4()), str(uuid4())
    try:
        with runtime.storage.execution_lock(), runtime.storage.daemon_owner_lock(other):
            runtime.storage.start_dispatch(stale,runtime.config,NOW)
            assert runtime.storage.runner_active()
            assert runtime.storage.daemon_owner_active(other)
            assert not runtime.storage.daemon_owner_active(stale)
            with pytest.raises(DispatchError, match='no executing daemon'):
                submit(runtime)
            assert runtime.storage.dispatch_status()['requests'] == []
        assert not browser.launches
    finally:
        runtime.close()


@pytest.mark.parametrize('malformed', ['../runner', '', None, 'F409CA03-28FA-43DB-9F4F-C1E1E8339825'])
def test_invalid_owner_record_cannot_select_a_lock_path(tmp_path, malformed):
    storage = SQLiteStorage(tmp_path/'synthetic.sqlite3')
    assert storage.daemon_owner_active(malformed) is False
    assert not list(tmp_path.glob('*.daemon-*.lock'))


def test_owner_lock_is_private_and_crash_leftover_file_is_not_liveness(tmp_path):
    storage = SQLiteStorage(tmp_path/'synthetic.sqlite3')
    owner = str(uuid4())
    with storage.daemon_owner_lock(owner):
        locks = list(tmp_path.glob('*.daemon-*.lock'))
        assert len(locks) == 1 and locks[0].stat().st_mode & 0o777 == 0o600
        assert locks[0].stat().st_size == 0 and storage.daemon_owner_active(owner)
    assert not locks[0].exists() and not storage.daemon_owner_active(owner)
    locks[0].touch(mode=0o600)  # Empty residue of os._exit; no owner holds the fd.
    assert not storage.daemon_owner_active(owner)
