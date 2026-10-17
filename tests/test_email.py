"""Optional SMTP delivery stays off unless configured and never opens a real mailbox."""

from __future__ import annotations

import io
import json
import re
import smtplib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from keepflame.capabilities import CapabilityRegistry, FileSecretResolver
from keepflame.capabilities.secrets import SecretReadError
from keepflame.cli import main
from keepflame.core.config import ConfigError, load_config
from keepflame.core.models import HumanAction, KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.service import SchedulerService
from keepflame.notifier.email import MailDeliveryError, deliver, smtp_connect
from keepflame.notifier.jsonl import JsonlNotifier
from keepflame.notifier.lifecycle import ConfigView, LifecycleNotifier
from keepflame.runtime import build_runtime
from keepflame.storage import SQLiteStorage
from tests.helpers import FixedClock, SequenceKeeper, StaticRegistry, StaticSecrets, write_secret
from tests.test_orchestrator import local

PASSWORD = "MAILBOX-PASSWORD-SENTINEL"
HIDDEN = "NOT-IN-MAIL-BODY"
ROOT = Path(__file__).resolve().parents[1]


class RecordingSMTP:
    def __init__(self, messages: list, *, fail: bool = False) -> None:
        self.messages = messages
        self.fail = fail
        self.password = None

    def login(self, user, password):
        self.password = password
        if self.fail:
            raise RuntimeError(f"authentication failed for {password}")

    def send_message(self, message):
        self.messages.append(message)

    def quit(self):
        return None


def _toml(storage: Path, password_file: str, *, mail: bool | None, keeper: str, enabled: bool, task: str = "checkin") -> str:
    notify = ""
    if mail is not None:
        notify = f"""
[notify.email]
enabled = {"true" if mail else "false"}
host = "smtp.example.invalid"
port = 2525
security = "starttls"
username = "keepflame@example.invalid"
password_file = {json.dumps(password_file)}
from = "keepflame@example.invalid"
to = ["operator@example.invalid"]
timeout_seconds = 12
"""
    return f"""
[storage]
path = {json.dumps(str(storage))}

[[flames]]
id = "joinquant-primary"
keeper = "{keeper}"
account_id = "primary"
enabled = {"true" if enabled else "false"}

[flames.schedule]
timezone = "Asia/Shanghai"
first_check = "09:00"
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"

[flames.settings]
task = "{task}"
{notify}
"""


def _write(tmp_path: Path, name: str, *, mail: bool | None, keeper: str = "fake", enabled: bool = True,
           password_file: Path | None = None, task: str = "checkin") -> Path:
    if password_file is None:
        password_file = tmp_path / f"{name}.smtp"
        write_secret(password_file, PASSWORD + "\n")
    path = tmp_path / f"{name}.toml"
    path.write_text(_toml(tmp_path / f"{name}.sqlite3", str(password_file), mail=mail, keeper=keeper, enabled=enabled, task=task))
    return path


def _service(tmp_path: Path, name: str, results, *, mail=True, connect=None, current=None, password_file=None, keeper="fake"):
    path = _write(tmp_path, name, mail=mail, keeper=keeper, password_file=password_file)
    config = load_config(path)
    stream = io.StringIO()
    messages: list = []
    clients: list[RecordingSMTP] = []

    def default_connect(_email):
        client = RecordingSMTP(messages)
        clients.append(client)
        return client

    clock = FixedClock(current or local(11, 9))
    storage = SQLiteStorage(config.storage.path)
    view = ConfigView(config)
    notifier = LifecycleNotifier(JsonlNotifier(stream), view, FileSecretResolver(), connect=connect or default_connect)
    keeper_obj = SequenceKeeper(*results)
    runner = Orchestrator(
        storage=storage, registry=StaticRegistry(keeper_obj), clock=clock,
        capabilities=CapabilityRegistry({}), secrets=StaticSecrets("unused"), notifier=notifier,
    )
    service = SchedulerService(config=config, storage=storage, orchestrator=runner, clock=clock)
    return SimpleNamespace(
        path=path, config=config, stream=stream, messages=messages, clients=clients, clock=clock,
        storage=storage, service=service, runner=runner, keeper=keeper_obj, view=view,
    )


def _tick(case, when):
    case.clock.current = when
    return case.service.run_due()


def _jsonl(case) -> list[dict]:
    return [json.loads(line) for line in case.stream.getvalue().splitlines() if line.strip()]


def _body(message) -> str:
    return message.get_content()


def _human(url: str | None = "https://example.invalid/maintain") -> KeepResult:
    return KeepResult(
        KeepStatus.WAITING_HUMAN, "needs a person",
        metadata={"detail": HIDDEN},
        human_action=HumanAction("REFRESH_SESSION", "需要登录", "在官方页面完成这一步", url),
    )


def _assert_identity(message, record: dict) -> str:
    body = _body(message)
    for field in ("flame_id", "account_id", "event", "status", "message", "notification_id"):
        assert record[field] in body
    assert record["next_action_at"] in body
    assert HIDDEN not in body
    assert PASSWORD not in body
    assert PASSWORD not in message.as_string()
    assert "credit_line" not in body
    assert record["event"] in message["Subject"]
    assert record["flame_id"] in message["Subject"]
    return body


def test_omitted_or_disabled_mail_keeps_jsonl_and_does_not_touch_transport(tmp_path: Path) -> None:
    outside = Path("/etc/keepflame/smtp.password")

    def boom(_email):
        raise AssertionError("mail transport contacted")

    for name, mail in (("omitted", None), ("disabled", False)):
        case = _service(tmp_path, name, [_human()], mail=mail, connect=boom, password_file=outside)
        _tick(case, local(11, 9))
        assert [item["event"] for item in _jsonl(case)] == ["human_action_required"]
        assert case.storage.get_notification("joinquant-primary", "primary") is None
        assert case.config.email is None or case.config.email.enabled is False
        assert PASSWORD.encode() not in case.storage.path.read_bytes()


def test_enabled_mail_sends_each_lifecycle_event_once_and_keeps_jsonl(tmp_path: Path) -> None:
    human = _service(tmp_path, "human", [_human(), _human()])
    _tick(human, local(11, 9))
    _tick(human, local(11, 20))

    credit = _service(tmp_path, "credit", [KeepResult(
        KeepStatus.SUCCESS, "积分已入账",
        metadata={"detail": HIDDEN, "credit_line": {
            "proven": True, "amount": 17, "item": "阅读样本积分",
            "at": "2026-08-11T10:09:15+08:00", "extra": HIDDEN,
        }},
    )])
    _tick(credit, local(11, 10))

    recovered = _service(tmp_path, "recovered", [
        KeepResult(KeepStatus.FAILED, "offline", metadata={"detail": HIDDEN}),
        KeepResult(KeepStatus.SUCCESS, "healthy again", metadata={"detail": HIDDEN}),
    ])
    _tick(recovered, local(11, 9))
    _tick(recovered, local(11, 10))

    paired = []
    for case in (human, credit, recovered):
        records = _jsonl(case)
        assert len(records) == len(case.messages)
        for record, message in zip(records, case.messages):
            body = _assert_identity(message, record)
            paired.append((record["event"], body, case))
            assert case.clients[-1].password == PASSWORD
        assert HIDDEN in case.stream.getvalue()
        assert PASSWORD not in case.stream.getvalue()
        assert PASSWORD.encode() not in case.storage.path.read_bytes()
        assert case.storage.get_notification("joinquant-primary", "primary") is None

    events = [event for event, _body_text, _case in paired]
    # Unpaused FAILED runs retry silently within the bounded ladder; no flame_failed here.
    assert events == [
        "human_action_required", "human_action_reminder", "credit_recorded", "flame_recovered",
    ]
    required, reminder, credit_body, _recovered_body = (item[1] for item in paired)
    for body in (required, reminder):
        assert "REFRESH_SESSION" in body
        assert "需要登录" in body
        assert "在官方页面完成这一步" in body
        assert "https://example.invalid/maintain" in body
    assert "阅读样本积分" in credit_body
    assert "2026-08-11T10:09:15+08:00" in credit_body
    assert re.search(r"\b17\b", credit_body)


def test_transport_failure_keeps_budget_and_status_reports_exhaustion(tmp_path: Path) -> None:
    calls = []

    def connect(_email):
        calls.append("send")
        return RecordingSMTP([], fail=True)

    case = _service(tmp_path, "budget", [_human()], connect=connect, keeper="joinquant")
    _tick(case, local(11, 9))
    for minute in (1, 3, 7, 15):
        _tick(case, local(11, 9) + timedelta(minutes=minute))
    assert len(calls) == 5
    pending = case.storage.get_notification("joinquant-primary", "primary")
    assert pending.event == "human_action_required"
    assert pending.attempts == 5
    assert pending.retry_at is None
    assert pending.last_error_type == "MailDeliveryError"
    assert pending.result.message == "needs a person"
    _tick(case, local(11, 9) + timedelta(minutes=16))
    assert len(calls) == 5
    assert PASSWORD.encode() not in case.storage.path.read_bytes()


def test_status_json_reports_exhausted_mail(tmp_path: Path, capsys) -> None:
    def connect(_email):
        return RecordingSMTP([], fail=True)

    case = _service(tmp_path, "status", [_human()], connect=connect, keeper="joinquant")
    _tick(case, local(11, 9))
    for minute in (1, 3, 7, 15):
        _tick(case, local(11, 9) + timedelta(minutes=minute))
    assert main(["status", "--config", str(case.path), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    note = payload["flames"][0]["notification"]
    assert note["event"] == "human_action_required"
    assert note["attempts"] == 5
    assert note["retry_at"] is None
    assert note["delivery_exhausted"] is True
    assert note["last_error_type"] == "MailDeliveryError"
    assert PASSWORD not in captured.out


def test_paused_flame_still_retries_pending_mail(tmp_path: Path) -> None:
    calls = []

    def connect(_email):
        calls.append("send")
        return RecordingSMTP([], fail=len(calls) == 1)

    paused = KeepResult(
        KeepStatus.WAITING_HUMAN, "login required",
        metadata={"automation_paused": True, "reason": "login_required", "detail": HIDDEN},
        human_action=HumanAction("REFRESH_SESSION", "登录", "在官方页面登录", "https://example.invalid/login"),
    )
    case = _service(tmp_path, "paused", [paused], connect=connect)
    _tick(case, local(11, 9))
    assert case.storage.is_paused("fake", "primary", "joinquant-primary")
    assert case.storage.count_runs() == 1
    assert case.storage.get_notification("joinquant-primary", "primary").attempts == 1
    _tick(case, local(11, 9) + timedelta(seconds=60))
    assert len(calls) == 2
    assert case.storage.count_runs() == 1
    assert case.keeper.results == [] or not case.keeper.results
    assert case.storage.get_notification("joinquant-primary", "primary") is None
    assert case.storage.is_paused("fake", "primary", "joinquant-primary")


def test_unreadable_password_is_a_delivery_failure_and_leaves_the_keeper_result(tmp_path: Path) -> None:
    contacted = []

    def connect(_email):
        contacted.append("send")
        return RecordingSMTP([])

    missing = tmp_path / "missing-smtp-password"
    case = _service(tmp_path, "missing", [_human()], password_file=missing, connect=connect)
    _tick(case, local(11, 9))
    state = case.storage.get_state("joinquant-primary", "primary")
    pending = case.storage.get_notification("joinquant-primary", "primary")
    assert state.status is KeepStatus.WAITING_HUMAN
    assert state.message == "needs a person"
    assert pending.attempts == 1
    assert pending.last_error_type == "MailDeliveryError"
    assert pending.result.status is KeepStatus.WAITING_HUMAN
    assert contacted == []

    empty = tmp_path / "empty.smtp"
    write_secret(empty, "\n")
    empty_case = _service(tmp_path, "empty", [_human()], password_file=empty, connect=connect)
    _tick(empty_case, local(11, 9))
    assert empty_case.storage.get_state("joinquant-primary", "primary").message == "needs a person"
    assert empty_case.storage.get_notification("joinquant-primary", "primary").last_error_type == "MailDeliveryError"
    assert contacted == []


def test_process_events_are_not_mailed(tmp_path: Path) -> None:
    calls = []

    def connect(_email):
        calls.append("mail")
        raise AssertionError("process event must not open mail")

    path = _write(tmp_path, "process", mail=True, keeper="fake", enabled=False)
    stream = io.StringIO()
    runtime = build_runtime(path, stream=stream, mail_connect=connect)
    try:
        assert isinstance(runtime.orchestrator.notifier, LifecycleNotifier)
        assert runtime.notifier is runtime.orchestrator.notifier.jsonl
        runtime.notifier.emit("service_started", config="synthetic", enabled_flames=0)
        runtime.notifier.emit("daemon_owner_started", owner_id="synthetic")
        runtime.notifier.emit("configuration_reloaded", request_id="synthetic")
        runtime.notifier.emit("daemon_run_started", request_id="synthetic", flame_id="joinquant-primary")
        runtime.notifier.emit("daemon_run_finished", request_id="synthetic", state="completed")
        runtime.notifier.emit("configuration_reload_failed", error_type="ConfigError")
        runtime.notifier.emit("daemon_owner_stopped", owner_id="synthetic")
        runtime.notifier.emit("service_stopped")
        text = stream.getvalue()
        for event in (
            "service_started", "daemon_owner_started", "configuration_reloaded",
            "daemon_run_started", "daemon_run_finished", "configuration_reload_failed",
            "daemon_owner_stopped", "service_stopped",
        ):
            assert event in text
        assert calls == []
    finally:
        runtime.close()


def test_reload_adopts_mail_config_without_sending(tmp_path: Path) -> None:
    calls = []

    def connect(_email):
        calls.append("mail")

    path = _write(tmp_path, "reload", mail=False, keeper="fake", enabled=False)
    runtime = build_runtime(path, stream=io.StringIO(), mail_connect=connect)
    try:
        runtime.storage.request_reload(datetime.now(timezone.utc))
        path.write_text(_toml(
            runtime.config.storage.path, str(tmp_path / "reload.smtp"),
            mail=True, keeper="fake", enabled=False,
        ))
        runtime.poll_controls()
        assert runtime.config.email is not None and runtime.config.email.enabled is True
        assert runtime.config_view.config.email.enabled is True
        commands = runtime.storage.runtime_commands(include_finished=True)
        assert commands[0]["state"] == "completed"
        assert calls == []
    finally:
        runtime.close()


def test_turning_mail_on_after_load_sends_the_next_lifecycle_event(tmp_path: Path, monkeypatch) -> None:
    reads = []
    current = FileSecretResolver.read_text

    def spy(self, reference):
        reads.append(reference)
        return current(self, reference)

    monkeypatch.setattr(FileSecretResolver, "read_text", spy)
    case = _service(tmp_path, "later", [_human(), _human()], mail=False, password_file=tmp_path / "later.smtp")
    write_secret(tmp_path / "later.smtp", PASSWORD + "\n")
    _tick(case, local(11, 9))
    assert reads == []
    assert [item["event"] for item in _jsonl(case)] == ["human_action_required"]
    enabled = load_config(_write(tmp_path, "later-on", mail=True, password_file=tmp_path / "later.smtp"))
    case.view.config = enabled
    _tick(case, local(11, 20))
    assert [item["event"] for item in _jsonl(case)] == ["human_action_required", "human_action_reminder"]
    assert len(case.messages) == 1
    assert "human_action_reminder" in _body(case.messages[0])
    assert reads == [str(tmp_path / "later.smtp")]


def test_smtp_connect_uses_the_configured_timeout_and_hides_the_password(tmp_path: Path, monkeypatch) -> None:
    created = []

    class FakeSMTP:
        def __init__(self, host="", port=0, local_hostname=None, timeout=None, source_address=None):
            created.append(("plain", host, port, timeout))
            self.started = False

        def starttls(self, *args, **kwargs):
            self.started = True
            created.append(("starttls",))

        def login(self, user, password):
            created.append(("login", user, password))

        def send_message(self, message):
            created.append(("message", message))

        def quit(self):
            created.append(("quit",))

    class FakeSSL:
        def __init__(self, host="", port=0, local_hostname=None, timeout=None, source_address=None, context=None):
            created.append(("ssl", host, port, timeout))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    path = _write(tmp_path, "timeout", mail=True)
    email = load_config(path).email
    assert email.timeout_seconds == 12
    assert email.port == 2525
    deliver(
        email, FileSecretResolver(), "KeepFlame synthetic", "body without the mailbox secret\n",
        notification_id="synthetic-id",
    )
    assert ("plain", email.host, email.port, email.timeout_seconds) in created
    assert ("starttls",) in created
    assert ("login", email.username, PASSWORD) in created
    message = next(item[1] for item in created if item[0] == "message")
    assert PASSWORD not in message.as_string()
    assert ("quit",) in created

    created.clear()
    smtp_connect(replace(email, security="ssl"))
    assert created == [("ssl", email.host, email.port, email.timeout_seconds)]
    created.clear()
    smtp_connect(replace(email, security="none"))
    assert created == [("plain", email.host, email.port, email.timeout_seconds)]


def test_delivery_exception_text_omits_the_password(tmp_path: Path) -> None:
    email = load_config(_write(tmp_path, "redact", mail=True)).email

    def explode(_email):
        raise RuntimeError(PASSWORD)

    with pytest.raises(MailDeliveryError) as caught:
        deliver(email, FileSecretResolver(), "subject", "body\n", notification_id="synthetic-id", connect=explode)
    assert PASSWORD not in str(caught.value)
    assert PASSWORD not in repr(caught.value)

    class BadLogin:
        def login(self, _user, password):
            raise RuntimeError(password)

        def send_message(self, _message):
            raise AssertionError("message was sent")

        def quit(self):
            raise RuntimeError(PASSWORD)

    with pytest.raises(MailDeliveryError) as caught:
        deliver(email, FileSecretResolver(), "subject", "body\n", notification_id="synthetic-id", connect=lambda _email: BadLogin())
    assert PASSWORD not in str(caught.value)


def test_mail_settings_fail_at_load_with_actionable_errors(tmp_path: Path) -> None:
    base = _toml(tmp_path / "state.sqlite3", str(tmp_path / "smtp.password"), mail=True, keeper="fake", enabled=False)
    path = tmp_path / "keepflame.toml"

    def load(text: str):
        path.write_text(text)
        return load_config(path)

    omitted = base.split("[notify.email]", 1)[0]
    assert load(omitted).email is None

    with pytest.raises(ConfigError, match="unknown notify.email keys: extra"):
        load(base.replace("timeout_seconds = 12", "timeout_seconds = 12\nextra = true"))
    with pytest.raises(ConfigError, match="unknown notify keys: extra"):
        load(base.replace("[notify.email]", "[notify]\nextra = true\n\n[notify.email]"))
    with pytest.raises(ConfigError, match="unknown top-level keys: mailbox"):
        load(base + "\n[mailbox]\nenabled = false\n")
    with pytest.raises(ConfigError, match="password_file must be an absolute path"):
        load(base.replace(str(tmp_path / "smtp.password"), "secrets/smtp.password"))
    with pytest.raises(ConfigError, match="security must be starttls, ssl, or none"):
        load(base.replace('security = "starttls"', 'security = "tls"'))
    with pytest.raises(ConfigError, match="port must be an integer"):
        load(base.replace("port = 2525", "port = true"))
    with pytest.raises(ConfigError, match="to must list at least one address"):
        load(base.replace('to = ["operator@example.invalid"]', "to = []"))
    with pytest.raises(ConfigError, match="timeout_seconds must be a number from 1 to 120"):
        load(base.replace("timeout_seconds = 12", "timeout_seconds = 0"))
    with pytest.raises(ConfigError, match="host must be a single-line value"):
        load(base.replace('host = "smtp.example.invalid"', 'host = ""'))
    enabled = load(base)
    assert enabled.email.enabled is True
    assert enabled.email.password_file == tmp_path / "smtp.password"
    assert load(base.replace("timeout_seconds = 12\n", "")).email.timeout_seconds == 15
    disabled = load(base.replace("enabled = true", "enabled = false"))
    assert disabled.email.enabled is False


def test_published_examples_keep_mail_off_without_a_secret_or_real_recipient() -> None:
    examples = [
        ROOT / "config/keepflame.example.toml",
        ROOT / "config/keepflame.local.example.toml",
        ROOT / "config/keepflame.browser.example.toml",
        ROOT / "deploy/keepflame.server.toml.example",
    ]
    for path in examples:
        text = path.read_text()
        config = load_config(path)
        assert config.email is not None and config.email.enabled is False
        assert "example.invalid" in text
        assert "enabled = false" in text
        assert PASSWORD not in text
        assert all(flame.enabled is False for flame in config.flames)


def test_dry_run_is_stable_for_disabled_and_enabled_mail(tmp_path: Path, capsys) -> None:
    disabled = _write(tmp_path, "dry-off", mail=False, keeper="joinquant", enabled=False,
                      password_file=Path("/etc/keepflame/smtp.password"))
    enabled = _write(tmp_path, "dry-on", mail=True, keeper="joinquant", enabled=False,
                     password_file=tmp_path / "absent-smtp-password")
    for path in (disabled, enabled):
        assert main(["run", "--config", str(path)]) == 0
        first = capsys.readouterr().out
        assert main(["run", "--config", str(path)]) == 0
        second = capsys.readouterr().out
        assert first == second
        payload = json.loads(first)
        assert payload["event"] == "dry_run"
        assert payload["execution_enabled"] is False
        assert payload["database_opened"] is False
        assert payload["browser_opened"] is False
        assert payload["flames"][0]["id"] == "joinquant-primary"


def test_secret_read_error_is_not_reported_with_file_contents(tmp_path: Path) -> None:
    email = load_config(_write(tmp_path, "guard", mail=True)).email

    class Broken(FileSecretResolver):
        def read_text(self, reference):
            raise SecretReadError(PASSWORD)

    with pytest.raises(MailDeliveryError) as caught:
        deliver(email, Broken(), "subject", "body\n", notification_id="synthetic-id", connect=lambda _email: None)
    assert PASSWORD not in str(caught.value)
