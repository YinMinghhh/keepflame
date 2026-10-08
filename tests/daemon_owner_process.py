"""Bounded child-process fixture; never starts a browser or external connection."""

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import select
import sys
from types import SimpleNamespace


mode, config_argument = sys.argv[1:3]
config_path = Path(config_argument).resolve()
root = config_path.parent


def audit(event, args):
    if event in {'socket.connect','socket.getaddrinfo','socket.sendto','socket.bind','subprocess.Popen'}:
        raise AssertionError('no child browser or network in owner fixture')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(part in str(path) for part in ('/Library/Application Support/', '/Library/Keychains/', '/Library/Cookies/')):
            raise AssertionError('no real account files in owner fixture')
        access, flags = args[1:3]
        writing = (isinstance(access,str) and any(v in access for v in 'wa+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
        if writing and not path.is_relative_to(root):
            raise AssertionError('owner fixture writes must remain in its temp directory')


sys.addaudithook(audit)
from keepflame import cli
from keepflame import runtime as runtime_module
from keepflame.core.models import KeepResult, KeepStatus

NOW = datetime(2026, 10, 8, 2, tzinfo=timezone.utc)


class Clock:
    def now(self):
        return NOW


class CommandClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def wait_for_parent():
    assert select.select([sys.stdin], [], [], 10)[0], 'fixture parent handshake timed out'
    assert sys.stdin.readline().strip() == 'GO'


class Keeper:
    def keep(self, context):
        if mode == 'standalone':
            print(json.dumps({'ready':True, 'kind':'standalone'}), flush=True)
            wait_for_parent()
        return KeepResult(KeepStatus.SUCCESS, 'synthetic owner fixture completed')


runtime_module.SystemClock = Clock
runtime_module.EntryPointKeeperRegistry = lambda: SimpleNamespace(resolve=lambda _: Keeper())
runtime_module.BrowserPool = lambda: SimpleNamespace(close=lambda: None)
cli.datetime = CommandClock

if mode == 'crash_daemon':
    runtime = runtime_module.build_runtime(config_path, stream=io.StringIO())
    with runtime.daemon_session():
        print(json.dumps({'ready':True, 'kind':'daemon', 'owner_id':runtime.dispatch_owner}), flush=True)
        wait_for_parent()
        os._exit(76)  # Only this fixture child; emulate a crash without finally.
elif mode == 'standalone':
    raise SystemExit(cli.main(['run-once','--execute','--config',str(config_path),'--flame','joinquant-primary']))
elif mode == 'client':
    raise SystemExit(cli.main(['run-once','--via-daemon','--execute','--config',str(config_path),
                              '--flame','reading','--request-id',sys.argv[3]]))
else:
    raise AssertionError('unknown owner fixture mode')
