"""Manual acceptance while the service runs: forced daemon runs and copy-ready human commands.

Fake keepers, fake HTTP and temporary databases only; nothing contacts a real service.
"""

import io
import json
import shlex
from datetime import timedelta
from uuid import uuid4

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.capabilities.secrets import FileSecretResolver
from keepflame.cli import main
from keepflame.core.models import KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.notifier.slack import render_lifecycle_message
from keepflame.runtime import build_runtime
from keepflame.storage.dispatch import DispatchError
from tests import test_daemon_dispatch as dispatch
from tests import test_ikuuu as ikuuu
from tests import test_tushare as tushare
from tests.helpers import FakeNotifier, FixedClock, MemoryStorage, StaticRegistry
from tests.test_config import write_config

SERVER_CONFIG = "/Users/yinming/Services/keepflame/keepflame server.toml"  # space: must be shell-quoted
WINDOWED = dispatch.CONFIG.replace('first_check = "09:00"', 'first_window_start = "11:00"\nfirst_window_end = "12:00"')


# --- 1. human-action commands name the running config ---------------------------------

def _orchestrator(keeper, http, now, config_path):
    return Orchestrator(storage=MemoryStorage(), registry=StaticRegistry(keeper),
                        capabilities=CapabilityRegistry({"http": http}), secrets=FileSecretResolver(),
                        notifier=FakeNotifier(), clock=FixedClock(now), config_path=config_path)


def _slack(result, flame_id):
    return render_lifecycle_message(event="human_action_required", notification_id="n", flame_id=flame_id,
                                    account_id="primary", result=result)


def test_tushare_login_alert_includes_the_quoted_daemon_config(tmp_path):
    tushare.store_cookie(tmp_path)
    runner = _orchestrator(tushare.TushareKeeper(sleeper=lambda _s: None),
                           tushare.FakeHttp({("GET", tushare.TASKS_URL): tushare.GONE}), tushare.NOW, SERVER_CONFIG)
    outcome = runner.run_flame(tushare.flame(tmp_path, "sign"), now=tushare.NOW)
    result = outcome.record.result
    command = f"pbpaste | keepflame tushare import-cookie --config {shlex.quote(SERVER_CONFIG)} --flame tushare-sign"
    assert result.status is KeepStatus.WAITING_HUMAN and command in result.human_action.instructions
    assert shlex.split(command.split("| ", 1)[1])[4] == SERVER_CONFIG
    text = _slack(result, "tushare-sign")
    assert command in text  # not truncated by the Slack field limit
    assert "SYNTHETIC" not in text + json.dumps(result.metadata, default=str)


def test_ikuuu_login_alert_includes_the_quoted_daemon_config(tmp_path):
    ikuuu.store_cookie(tmp_path)
    routes = ikuuu.nav_routes()
    routes[("POST", "https://ikuuu.pw/user/checkin")] = ikuuu.checkin_json({"ret": -1, "msg": "请登录后再操作"})
    runner = _orchestrator(ikuuu.IkuuuKeeper(sleeper=lambda _s: None), ikuuu.FakeHttp(routes), ikuuu.NOW, SERVER_CONFIG)
    result = runner.run_flame(ikuuu.flame(tmp_path), now=ikuuu.NOW).record.result
    command = (f"pbpaste | keepflame ikuuu import-cookie --config {shlex.quote(SERVER_CONFIG)} "
               "--flame ikuuu-primary --base https://ikuuu.pw")
    assert command in result.human_action.instructions and command in _slack(result, "ikuuu-primary")
    assert "SYNTHETIC-SECRET-KEY" not in _slack(result, "ikuuu-primary")


def test_runtime_passes_its_resolved_config_path_to_keepers(tmp_path):
    path = write_config(tmp_path, dispatch.CONFIG)
    runtime = build_runtime(path, browser=object(), stream=io.StringIO())
    try:
        assert runtime.orchestrator.config_path == str(path.resolve())
    finally:
        runtime.close()


def test_without_a_known_config_the_command_is_unchanged(tmp_path):
    tushare.store_cookie(tmp_path)
    runner = _orchestrator(tushare.TushareKeeper(sleeper=lambda _s: None),
                           tushare.FakeHttp({("GET", tushare.TASKS_URL): tushare.GONE}), tushare.NOW, None)
    result = runner.run_flame(tushare.flame(tmp_path, "sign"), now=tushare.NOW).record.result
    assert "import-cookie --flame tushare-sign" in result.human_action.instructions
    assert "--config" not in result.human_action.instructions


# --- 2. forced daemon runs -------------------------------------------------------------

class Scripted:
    """Returns queued statuses; counts calls."""

    def __init__(self, *statuses, only="joinquant-primary"):
        self.statuses = list(statuses)
        self.only = only
        self.calls = 0

    def keep(self, context):
        if context.flame_id != self.only:  # the config's second Flame shares this keeper
            return KeepResult(KeepStatus.SUCCESS, "synthetic other flame")
        self.calls += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return KeepResult(status, f"synthetic {status.value.lower()}")


def _setup(tmp_path, monkeypatch, keeper, config=dispatch.CONFIG):
    monkeypatch.setattr(dispatch, "CONFIG", config)
    runtime, browser, pool, visits, clock, stream = dispatch.setup(tmp_path, monkeypatch)
    runtime.orchestrator.registry = StaticRegistry(keeper)
    return runtime, clock, stream


def _submit(runtime, forced, identifier=None):
    return runtime.storage.submit_run(identifier or str(uuid4()), runtime.config, runtime.config.flames[0],
                                      runtime.orchestrator.clock.now(), forced=forced)


def _events(stream, name):
    return [json.loads(line) for line in stream.getvalue().splitlines() if f'"{name}"' in line]


def test_forced_request_runs_a_flame_that_is_not_due_and_records_it(tmp_path, monkeypatch):
    keeper = Scripted(KeepStatus.SUCCESS)
    runtime, clock, stream = _setup(tmp_path, monkeypatch, keeper)
    flame = runtime.config.flames[0]
    with runtime.daemon_session():
        runtime.orchestrator.run_flame(flame, notify=False)  # already succeeded today -> due tomorrow
        plain = _submit(runtime, False)
        runtime.poll_controls()
        assert dispatch.request(runtime, plain["id"])["reason"] == "not_due" and keeper.calls == 1
        forced = _submit(runtime, True)
        runtime.poll_controls()
        row = dispatch.request(runtime, forced["id"])
        assert row["state"] == "completed" and row["forced"] == 1 and keeper.calls == 2
        state = runtime.storage.get_state(flame.id, flame.account_id)
        assert state.metadata["forced"] is True and state.metadata["forced_ahead_of_schedule"] is True
        assert "forced_scheduled_due" in state.metadata
        assert state.next_action_at > dispatch.NOW + timedelta(hours=12)  # still tomorrow
        finished = _events(stream, "daemon_run_finished")[-1]
        assert finished["forced"] is True and finished["state"] == "completed"
        started = _events(stream, "daemon_run_started")[-1]
        assert started["forced"] is True and started["forced_ahead_of_schedule"] is True


def test_forced_failure_ahead_of_schedule_keeps_retry_budget_slot_and_window_draw(tmp_path, monkeypatch):
    keeper = Scripted(KeepStatus.FAILED)
    runtime, clock, stream = _setup(tmp_path, monkeypatch, keeper, WINDOWED)
    flame = runtime.config.flames[0]
    with runtime.daemon_session():
        due = runtime.service.planner.due(runtime.config, flame, None, dispatch.NOW)  # draws 11:00-12:00 local
        assert due > dispatch.NOW
        with runtime.storage._connect() as db:
            draws = [tuple(r) for r in db.execute("SELECT * FROM daily_first_runs")]
        _submit(runtime, True)
        runtime.poll_controls()
        state = runtime.storage.get_state(flame.id, flame.account_id)
        assert keeper.calls == 1 and state.status is KeepStatus.FAILED
        assert state.consecutive_failures == 0 and state.metadata["retry_consumed"] is False
        assert state.next_action_at == due  # the drawn slot is kept, not moved earlier or later
        assert "retries_exhausted" not in state.metadata
        with runtime.storage._connect() as db:
            assert [tuple(r) for r in db.execute("SELECT * FROM daily_first_runs")] == draws
        assert runtime.storage.get_notification(flame.id, flame.account_id) is None  # no alert from a probe
        # The scheduled run still happens at the drawn time and spends retries normally.
        clock.current = due
        runtime.service.run_due(now=due)
        state = runtime.storage.get_state(flame.id, flame.account_id)
        assert keeper.calls == 2 and state.consecutive_failures == 1


def test_forced_request_when_already_due_is_an_ordinary_run(tmp_path, monkeypatch):
    keeper = Scripted(KeepStatus.FAILED)
    runtime, clock, stream = _setup(tmp_path, monkeypatch, keeper)
    flame = runtime.config.flames[0]
    with runtime.daemon_session():
        _submit(runtime, True)
        runtime.poll_controls()
        state = runtime.storage.get_state(flame.id, flame.account_id)
        assert state.metadata["forced"] is True and state.metadata["forced_ahead_of_schedule"] is False
        assert state.consecutive_failures == 1 and "retry_consumed" not in state.metadata


@pytest.mark.parametrize("case", ["pause", "disabled"])
def test_force_never_bypasses_pause_or_enabled_gates(tmp_path, monkeypatch, case):
    keeper = Scripted(KeepStatus.SUCCESS)
    runtime, clock, stream = _setup(tmp_path, monkeypatch, keeper)
    flame = runtime.config.flames[0]
    with runtime.daemon_session():
        row = _submit(runtime, True)
        if case == "pause":
            runtime.storage.pause(flame.keeper, flame.account_id, "*", "synthetic pause", dispatch.NOW)
        else:
            runtime.config.source_path.write_text(dispatch.CONFIG.replace("enabled = true", "enabled = false"))
            runtime.storage.request_reload(dispatch.NOW)
        runtime.poll_controls()
        observed = dispatch.request(runtime, row["id"])
        assert observed["state"] == "rejected" and observed["reason"] in {"automation_paused", "flame_disabled_or_changed", "configuration_changed"}
        assert keeper.calls == 0


def test_request_id_cannot_switch_force_setting(tmp_path, monkeypatch):
    runtime, clock, stream = _setup(tmp_path, monkeypatch, Scripted(KeepStatus.SUCCESS))
    with runtime.daemon_session():
        row = _submit(runtime, False)
        with pytest.raises(DispatchError, match="--force"):
            _submit(runtime, True, row["id"])
        assert _submit(runtime, False, row["id"])["id"] == row["id"]


def test_cli_force_generates_a_request_id_and_prints_how_to_check(tmp_path, monkeypatch, capsys):
    keeper = Scripted(KeepStatus.SUCCESS)
    runtime, clock, stream = _setup(tmp_path, monkeypatch, keeper)
    path = runtime.config.source_path
    with runtime.daemon_session():
        assert main(["run-once", "--config", str(path), "--flame", "joinquant-primary",
                     "--via-daemon", "--force", "--execute"]) == 0
        record = json.loads(capsys.readouterr().out)
        assert record["forced"] is True and record["state"] == "requested"
        assert f"--request-id {record['request_id']} --force --execute" in record["check_with"]
        assert shlex.quote(str(path.resolve())) in record["check_with"]
        runtime.poll_controls()
        assert keeper.calls == 1
        assert main(shlex.split(record["check_with"])[1:]) == 0
        assert json.loads(capsys.readouterr().out)["execution_completed"] is True


def test_cli_force_without_a_daemon_suggests_a_standalone_run(tmp_path, monkeypatch, capsys):
    runtime, clock, stream = _setup(tmp_path, monkeypatch, Scripted(KeepStatus.SUCCESS))
    path = runtime.config.source_path
    runtime.close()
    monkeypatch.setattr("keepflame.cli.build_runtime", lambda *_: pytest.fail("no fallback run"))
    assert main(["run-once", "--config", str(path), "--flame", "joinquant-primary",
                 "--via-daemon", "--force", "--execute"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "DispatchError"
    assert f"keepflame run-once --config {shlex.quote(str(path.resolve()))} --flame joinquant-primary --execute" in error["message"]


def test_force_requires_via_daemon(tmp_path, capsys, monkeypatch):
    path = write_config(tmp_path, dispatch.CONFIG)
    monkeypatch.setattr("keepflame.cli.build_runtime", lambda *_: pytest.fail("must fail first"))
    assert main(["run-once", "--config", str(path), "--flame", "joinquant-primary", "--force", "--execute"]) == 1
    assert "--force requires --via-daemon" in capsys.readouterr().err
