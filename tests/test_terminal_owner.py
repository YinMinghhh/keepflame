"""Same-tick owner termination must stop even a later non-browser Keeper."""
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json

import pytest

from keepflame.capabilities.browser import BrowserRestoreFailed
from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.core.models import HumanAction, KeepResult, KeepStatus
from keepflame.runtime import build_runtime
from tests.helpers import FixedClock, StaticRegistry
from tests.test_runtime_control import Browser, OPTIONS

NOW = datetime(2026, 10, 8, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize('failure', ['restore', 'cleanup', 'ordinary', 'paused'])
def test_each_scheduled_flame_checks_terminal_owner_before_business_work(tmp_path, monkeypatch, failure):
    config = tmp_path/'config.toml'
    data = '[storage]\npath="synthetic.sqlite3"\n'
    for name in ('first', 'later'):
        data += f'''[[flames]]
id="{name}"
keeper="synthetic"
account_id="synthetic"
enabled=true
[flames.schedule]
timezone="UTC"
first_check="00:00"
recheck_minutes=60
reminder_at="22:00"
stop_at="23:59"
[flames.settings]
task="{name}"
'''
    config.write_text(data)
    visits, writes, contexts, launches = [], [], [], []

    class Source(Browser):
        @contextmanager
        def session(self, **options):
            launches.append(True)
            if failure == 'restore':
                raise BrowserRestoreFailed('browser restore startup failed')
            with super().session(**options) as owned:
                yield owned

    pool = BrowserPool(Source())

    class Keeper:
        def keep(self, context):
            visits.append(context.flame_id)
            if context.flame_id == 'first':
                with context.capabilities.require('browser').open(**OPTIONS) as page:
                    contexts.append(page.context)
                    if failure == 'cleanup':
                        def fail_close():
                            raise RuntimeError('synthetic security cleanup failure')
                        page.close = fail_close
                    if failure == 'ordinary':
                        raise ValueError('ordinary task failure with reusable context')
                    if failure == 'paused':
                        return KeepResult(KeepStatus.WAITING_HUMAN, 'task paused only',
                            metadata={'automation_paused':True, 'reason':'synthetic_task_pause'},
                            human_action=HumanAction('wait', 'Task paused', 'Synthetic verification only'))
                    return KeepResult(KeepStatus.SUCCESS, 'synthetic task body completed')
            # A terminal owner must stop this even though it needs NO browser.
            writes.append('synthetic_business_write')
            assert context.storage.reserve_action('synthetic', 'synthetic', 'later_write', '2026-10-08', NOW)
            if failure in ('ordinary', 'paused'):
                with context.capabilities.require('browser').open(**OPTIONS) as page:
                    contexts.append(page.context)
            return KeepResult(KeepStatus.SUCCESS, 'synthetic later task completed')

    monkeypatch.setattr('keepflame.runtime.BrowserPool', lambda:pool)
    monkeypatch.setattr('keepflame.runtime.EntryPointKeeperRegistry', lambda:StaticRegistry(Keeper()))
    monkeypatch.setattr('keepflame.runtime.SystemClock', lambda:FixedClock(NOW))
    runtime = build_runtime(config, stream=io.StringIO())
    store = runtime.storage
    assert store.reserve_action('synthetic','synthetic','held','2026-10-07',NOW)
    store.pause('synthetic','synthetic','held','existing_pause',NOW)
    terminal = failure in ('restore','cleanup')
    with runtime.daemon_session():
        outcomes, _ = runtime.service.run_due(now=NOW)
        observation = {
            'case':failure, 'visits_after_one_tick':visits[:], 'business_writes':writes[:],
            'pool_closed':pool.closed, 'pool_reason':pool.close_reason,
            'stop_event_after_tick':runtime.service.stop_event.is_set(),
            'runs_after_tick':store.count_runs(), 'outcome_statuses':[o.record.result.status.value for o in outcomes],
            'browser_launch_attempts':len(launches),
            'pending_preserved':store.get_action('synthetic','synthetic','held').state=='pending',
            'pause_preserved':store.is_paused('synthetic','synthetic','held'),
            'later_action_exists':store.get_action('synthetic','synthetic','later_write') is not None,
            'same_context_reused':len(contexts)==2 and contexts[0] is contexts[1],
        }
        (tmp_path/'terminal-owner-observation.json').write_text(json.dumps(observation,indent=2)+'\n')
        assert observation['pending_preserved'] and observation['pause_preserved']
        assert len(launches)==1
        if terminal:
            assert visits==['first']
            assert writes==[] and not observation['later_action_exists']
            assert observation['stop_event_after_tick'] and pool.closed
            assert len(outcomes)==store.count_runs()==1
            assert outcomes[0].record.result.status is KeepStatus.FAILED
            # No next poll is needed to stop this same scheduler tick, or later ticks.
            assert runtime.service.run_due(now=NOW)[0]==[]
            assert visits==['first'] and writes==[]
        else:
            assert visits==['first','later'] and len(writes)==1
            assert not pool.closed and not runtime.service.stop_event.is_set()
            assert observation['same_context_reused']
            assert len(outcomes)==store.count_runs()==2
            assert outcomes[0].record.result.status is (KeepStatus.FAILED if failure=='ordinary' else KeepStatus.WAITING_HUMAN)
            assert store.is_paused('synthetic','synthetic','first') is (failure=='paused')
            assert outcomes[1].record.result.status is KeepStatus.SUCCESS
