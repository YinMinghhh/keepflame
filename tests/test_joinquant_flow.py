"""The CLI-to-storage human-sign-in loop; no sockets or real credentials."""

import io
import json
from datetime import timedelta
from email.utils import format_datetime

import pytest

from keepflame import cli
from keepflame.core.models import KeepStatus
from keepflame.capabilities.http import HttpTransportError
from keepflame.runtime import build_runtime
from tests.helpers import FakeHttpClient, FakeNotifier, FixedClock, response
from tests.test_cli import config_file
from tests.test_joinquant import run_keeper
from tests.test_orchestrator import local


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_skips_immediate_retry_and_preserves_server_cooldown(status):
    now = local(11, 9)
    http = FakeHttpClient(response(status, "busy", headers={"Retry-After": "172800"}))
    result = run_keeper(http)
    assert len(http.requests) == 1
    assert result.status is KeepStatus.FAILED
    assert result.next_action_at == now + timedelta(days=2)


def test_http_date_retry_after_is_understood():
    instant = local(12, 9)
    result = run_keeper(FakeHttpClient(response(429, "busy", headers={"Retry-After": format_datetime(instant, usegmt=True)})))
    assert result.next_action_at == instant


def test_network_failures_stop_after_two_attempts():
    http = FakeHttpClient(HttpTransportError("synthetic"), HttpTransportError("synthetic"))
    assert run_keeper(http).status is KeepStatus.FAILED
    assert len(http.requests) == 2


@pytest.mark.parametrize("header", ["invalid", "-4", "9" * 100])
def test_malformed_retry_after_is_safe(header):
    result = run_keeper(FakeHttpClient(response(429, "busy", headers={"Retry-After": header})))
    assert result.status is KeepStatus.FAILED and result.next_action_at is None


def test_cli_joinquant_human_loop_survives_restart(tmp_path, monkeypatch, capsys):
    cookie = tmp_path / "synthetic.cookie"
    cookie.write_text("sid=synthetic-for-offline-test")
    config = config_file(tmp_path, cookie)
    http = FakeHttpClient(
        response(200, '{"code":"00000","data":{"taskList":{"login":{"status":0}}}}'),
        response(200, '{"code":"00000","data":{"taskList":{"login":{"status":0}}}}'),
        response(200, '{"code":"00000","data":{"taskList":{"login":{"status":1}}}}'),
        response(401, "synthetic expired identity"),
    )
    clock = FixedClock(local(11, 9))
    captured = FakeNotifier()
    monkeypatch.setattr("keepflame.runtime.UrllibHttpClient", lambda: http)
    monkeypatch.setattr("keepflame.runtime.SystemClock", lambda: clock)
    runtimes = []

    def factory(path):
        runtime = build_runtime(path, stream=io.StringIO(), delivery_notifier=captured)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(cli, "build_runtime", factory)
    assert cli.main(["run-once", "--execute", "--config", str(config)]) == 2
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "WAITING_HUMAN"
    assert first["human_action"]["kind"] == "COMPLETE_OFFICIAL_SIGNIN"
    assert captured.events == []

    # Exercise CLI run with a bounded fake daemon body, never run_forever or sleep.
    def one_tick(runtime):
        runtime.service.run_due()
        return 0

    monkeypatch.setattr(cli, "_run_daemon", one_tick)
    clock.current = local(11, 9) + timedelta(minutes=1)
    assert cli.main(["run", "--execute", "--config", str(config)]) == 0
    assert len(http.requests) == 1
    assert [e["event"] for e in captured.events] == ["human_action_required"]
    clock.current = local(11, 20)
    assert cli.main(["run", "--execute", "--config", str(config)]) == 0
    assert captured.events[-1]["event"] == "human_action_reminder"
    clock.current = local(11, 21)
    assert cli.main(["run", "--execute", "--config", str(config)]) == 0
    assert captured.events[-1]["event"] == "flame_recovered"
    assert cli.main(["status", "--config", str(config), "--json"]) == 0
    state = json.loads(capsys.readouterr().out)["flames"][0]
    assert state["status"] == "SUCCESS" and state["human_action"] is None
    assert state["notification"] is None
    clock.current = local(11, 22)
    assert cli.main(["run", "--execute", "--config", str(config)]) == 0
    assert len(http.requests) == 3
    clock.current = local(12, 9)
    assert cli.main(["run", "--execute", "--config", str(config)]) == 0
    assert captured.events[-1]["result"].human_action.kind == "REFRESH_SESSION"
    assert len(http.requests) == 4
    assert all(request.method == "GET" for request, _ in http.requests)
    assert all("getTaskInfo" in request.url for request, _ in http.requests)
    assert runtimes[-1].storage.count_runs() == 4


def test_cli_concurrent_runner_fails_before_secret_or_http(tmp_path, monkeypatch, capsys):
    config = config_file(tmp_path)
    http = FakeHttpClient()
    monkeypatch.setattr("keepflame.runtime.UrllibHttpClient", lambda: http)
    runtime = build_runtime(config)
    with runtime.storage.execution_lock():
        assert cli.main(["run-once", "--execute", "--config", str(config)]) == 1
        error = json.loads(capsys.readouterr().err)
        assert error["error_type"] == "StorageError"
    assert http.requests == []


@pytest.mark.parametrize("status", [True, False, 1.0, 0.0, None, 2, "true", "1.0"])
def test_ambiguous_remote_status_never_claims_success(status):
    payload = json.dumps({"code": "00000", "data": {"taskList": {"login": {"status": status}}}})
    result = run_keeper(FakeHttpClient(response(200, payload)))
    assert result.status is KeepStatus.FAILED
    assert result.reward is None
