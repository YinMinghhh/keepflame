"""Optional Slack webhook delivery stays off unless configured and never contacts Slack."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from keepflame.capabilities import CapabilityRegistry, FileSecretResolver
from keepflame.capabilities.secrets import SecretReadError
from keepflame.core.config import ConfigError, load_config
from keepflame.core.models import HumanAction, KeepResult, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.service import SchedulerService
from keepflame.notifier.jsonl import JsonlNotifier
from keepflame.notifier.lifecycle import ConfigView, LifecycleNotifier
from keepflame.notifier.slack import SlackDeliveryError, deliver, post_webhook, validated_webhook
from keepflame.runtime import build_runtime
from keepflame.storage import SQLiteStorage
from tests.helpers import FixedClock, SequenceKeeper, StaticRegistry, StaticSecrets
from tests.test_email import PASSWORD, RecordingSMTP
from tests.test_orchestrator import local

HOOK = "https://hooks.slack.com/services/TSYNTHETIC/BSYNTHETIC/SYNTHETIC-WEBHOOK-TOKEN"
HIDDEN = "NOT-IN-SLACK-TEXT"
COOKIE = "SESSION-COOKIE-SENTINEL"
TRANSPORT_DETAIL = "TRANSPORT-ERROR-DETAIL-SENTINEL"
ROOT = Path(__file__).resolve().parents[1]


class FakeSlack:
    """Injected transport: records (url, payload, timeout) and never opens a socket."""

    def __init__(self, *, status: int = 200, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict, float]] = []
        self.status = status
        self.fail = fail

    def __call__(self, url: str, payload: bytes, timeout: float) -> int:
        self.calls.append((url, json.loads(payload.decode("utf-8")), timeout))
        if self.fail:
            raise RuntimeError(f"cannot reach {url} {TRANSPORT_DETAIL}")
        return self.status

    @property
    def texts(self) -> list[str]:
        return [call[1]["text"] for call in self.calls]


def _toml(storage: Path, hook_file: str, *, slack: bool | None, mail: bool | None = None,
          keeper: str = "fake", timeout: str = "12", enabled: bool = False) -> str:
    notify = ""
    if mail is not None:
        notify += f"""
[notify.email]
enabled = {"true" if mail else "false"}
host = "smtp.example.invalid"
port = 2525
security = "starttls"
username = "keepflame@example.invalid"
password_file = {json.dumps(str(storage.parent / "mail.smtp"))}
from = "keepflame@example.invalid"
to = ["operator@example.invalid"]
timeout_seconds = 12
"""
    if slack is not None:
        notify += f"""
[notify.slack]
enabled = {"true" if slack else "false"}
webhook_file = {json.dumps(hook_file)}
timeout_seconds = {timeout}
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
task = "checkin"
{notify}
"""


def _write(tmp_path: Path, name: str, *, slack: bool | None, mail: bool | None = None,
           hook_file: Path | None = None, hook: str | None = HOOK, enabled: bool = False) -> Path:
    if hook_file is None:
        hook_file = tmp_path / f"{name}.webhook"
        if hook is not None:
            hook_file.write_text(hook + "\n")
    (tmp_path / "mail.smtp").write_text(PASSWORD + "\n")
    path = tmp_path / f"{name}.toml"
    path.write_text(_toml(tmp_path / f"{name}.sqlite3", str(hook_file), slack=slack, mail=mail,
                              enabled=enabled))
    return path


def _service(tmp_path: Path, name: str, results, *, slack=True, mail=None, post=None, connect=None,
             hook_file=None, hook: str | None = HOOK):
    path = _write(tmp_path, name, slack=slack, mail=mail, hook_file=hook_file, hook=hook, enabled=True)
    config = load_config(path)
    stream = io.StringIO()
    fake = post if post is not None else FakeSlack()
    messages: list = []

    def mail_connect(_email):
        return RecordingSMTP(messages)

    clock = FixedClock(local(11, 9))
    storage = SQLiteStorage(config.storage.path)
    view = ConfigView(config)
    notifier = LifecycleNotifier(JsonlNotifier(stream), view, FileSecretResolver(),
                                 connect=connect or mail_connect, slack_post=fake)
    keeper = SequenceKeeper(*results)
    runner = Orchestrator(
        storage=storage, registry=StaticRegistry(keeper), clock=clock,
        capabilities=CapabilityRegistry({}), secrets=StaticSecrets("unused"), notifier=notifier,
    )
    service = SchedulerService(config=config, storage=storage, orchestrator=runner, clock=clock)
    return SimpleNamespace(path=path, config=config, stream=stream, slack=fake, messages=messages,
                           clock=clock, storage=storage, service=service, view=view)


def _tick(case, when):
    case.clock.current = when
    return case.service.run_due()


def _jsonl(case) -> list[dict]:
    return [json.loads(line) for line in case.stream.getvalue().splitlines() if line.strip()]


def _human(url: str | None = "https://example.invalid/maintain?token=URL-TOKEN-SENTINEL") -> KeepResult:
    return KeepResult(
        KeepStatus.WAITING_HUMAN, "needs a person",
        metadata={"detail": HIDDEN, "page_excerpt": COOKIE},
        human_action=HumanAction("REFRESH_SESSION", "需要登录", "在官方页面完成这一步", url),
    )


def test_omitted_or_disabled_slack_keeps_jsonl_and_touches_neither_file_nor_network(tmp_path: Path,
                                                                                  monkeypatch) -> None:
    reads = []
    original = FileSecretResolver.read_text

    def spy(self, reference):
        reads.append(reference)
        return original(self, reference)

    monkeypatch.setattr(FileSecretResolver, "read_text", spy)
    outside = Path("/etc/keepflame/slack-webhook.url")
    for name, slack in (("omitted", None), ("disabled", False)):
        case = _service(tmp_path, name, [_human()], slack=slack, hook_file=outside)
        _tick(case, local(11, 9))
        assert [item["event"] for item in _jsonl(case)] == ["human_action_required"]
        assert case.slack.calls == []
        assert case.storage.get_notification("joinquant-primary", "primary") is None
        assert case.config.slack is None or case.config.slack.enabled is False
    assert reads == []


def test_enabled_slack_sends_one_short_redacted_message_per_event(tmp_path: Path) -> None:
    human = _service(tmp_path, "human", [_human(), _human()])
    _tick(human, local(11, 9))
    _tick(human, local(11, 20))
    credit = _service(tmp_path, "credit", [KeepResult(
        KeepStatus.SUCCESS, "积分已入账",
        metadata={"detail": HIDDEN, "page_excerpt": COOKIE, "credit_line": {
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

    seen = []
    for case in (human, credit, recovered):
        records = _jsonl(case)
        assert len(records) == len(case.slack.calls)
        for record, (url, payload, timeout) in zip(records, case.slack.calls):
            assert url == HOOK
            assert timeout == 12
            assert set(payload) == {"text"}
            text = payload["text"]
            seen.append((record["event"], text))
            for field in ("flame_id", "account_id", "event", "status", "message", "notification_id"):
                assert record[field] in text
            assert record["next_action_at"] in text
            for secret in (HOOK, "SYNTHETIC-WEBHOOK-TOKEN", HIDDEN, COOKIE, "URL-TOKEN-SENTINEL",
                           "credit_line", "http"):
                assert secret not in text
        assert HIDDEN in case.stream.getvalue()
        assert HOOK.encode() not in case.storage.path.read_bytes()
        assert case.storage.get_notification("joinquant-primary", "primary") is None

    assert [event for event, _text in seen] == [
        "human_action_required", "human_action_reminder", "credit_recorded", "flame_failed", "flame_recovered",
    ]
    required = seen[0][1]
    assert "REFRESH_SESSION" in required and "需要登录" in required and "在官方页面完成这一步" in required
    credit_text = seen[2][1]
    assert "阅读样本积分" in credit_text and "Credit-Amount: 17" in credit_text
    assert "Credit-Amount" not in seen[0][1] and "Credit-Amount" not in seen[3][1]


def test_message_escapes_slack_control_characters_and_is_bounded(tmp_path: Path) -> None:
    noisy = KeepResult(KeepStatus.FAILED, "<!channel> & <https://x.invalid|click>\n" + "x" * 2000)
    case = _service(tmp_path, "escape", [noisy])
    _tick(case, local(11, 9))
    text = case.slack.texts[0]
    assert "<" not in text and ">" not in text
    assert "&lt;!channel&gt; &amp; &lt;" in text
    assert "\n\n" not in text
    assert len(text) < 1000


def test_failure_retries_one_two_four_eight_minutes_and_stops_after_five(tmp_path: Path) -> None:
    fake = FakeSlack(fail=True)
    case = _service(tmp_path, "budget", [_human()], post=fake)
    _tick(case, local(11, 9))
    pending = case.storage.get_notification("joinquant-primary", "primary")
    assert pending.attempts == 1
    assert pending.retry_at == local(11, 9) + timedelta(minutes=1)
    for minute, wait in ((1, 2), (3, 4), (7, 8)):
        _tick(case, local(11, 9) + timedelta(minutes=minute))
        pending = case.storage.get_notification("joinquant-primary", "primary")
        assert pending.retry_at == local(11, 9) + timedelta(minutes=minute + wait)
    _tick(case, local(11, 9) + timedelta(minutes=15))
    assert len(fake.calls) == 5
    pending = case.storage.get_notification("joinquant-primary", "primary")
    assert pending.attempts == 5 and pending.retry_at is None
    assert pending.last_error_type == "SlackDeliveryError"
    _tick(case, local(11, 9) + timedelta(minutes=16))
    assert len(fake.calls) == 5
    stored = case.storage.path.read_bytes()
    assert HOOK.encode() not in stored and TRANSPORT_DETAIL.encode() not in stored
    assert HOOK not in case.stream.getvalue() and TRANSPORT_DETAIL not in case.stream.getvalue()


def test_retry_after_failure_delivers_once_and_clears_the_queue(tmp_path: Path) -> None:
    class Flaky(FakeSlack):
        def __call__(self, url, payload, timeout):
            self.fail = len(self.calls) == 0
            return super().__call__(url, payload, timeout)

    fake = Flaky()
    case = _service(tmp_path, "flaky", [_human()], post=fake)
    _tick(case, local(11, 9))
    first_id = case.storage.get_notification("joinquant-primary", "primary").notification_id
    _tick(case, local(11, 9) + timedelta(minutes=1))
    assert case.storage.get_notification("joinquant-primary", "primary") is None
    assert len(fake.calls) == 2
    assert all(first_id in text for text in fake.texts)
    _tick(case, local(11, 9) + timedelta(minutes=2))
    assert len(fake.calls) == 2


@pytest.mark.parametrize("status", [302, 400, 404, 429, 500])
def test_non_200_response_is_a_delivery_failure(tmp_path: Path, status: int) -> None:
    case = _service(tmp_path, f"status-{status}", [_human()], post=FakeSlack(status=status))
    _tick(case, local(11, 9))
    pending = case.storage.get_notification("joinquant-primary", "primary")
    assert pending.attempts == 1 and pending.last_error_type == "SlackDeliveryError"


def test_unreadable_empty_or_foreign_webhook_files_fail_delivery_without_contacting_slack(tmp_path: Path) -> None:
    missing = _service(tmp_path, "missing", [_human()], hook_file=tmp_path / "absent.webhook", hook=None)
    _tick(missing, local(11, 9))
    empty = _service(tmp_path, "empty", [_human()], hook="")
    _tick(empty, local(11, 9))
    foreign = _service(tmp_path, "foreign", [_human()], hook="https://example.invalid/services/T/B/SECRET-X")
    _tick(foreign, local(11, 9))
    for case in (missing, empty, foreign):
        state = case.storage.get_state("joinquant-primary", "primary")
        pending = case.storage.get_notification("joinquant-primary", "primary")
        assert state.status is KeepStatus.WAITING_HUMAN and state.message == "needs a person"
        assert pending.attempts == 1 and pending.last_error_type == "SlackDeliveryError"
        assert case.slack.calls == []
        assert b"SECRET-X" not in case.storage.path.read_bytes()


@pytest.mark.parametrize("address", [
    "http://hooks.slack.com/services/T/B/X",
    "https://hooks.slack.com",
    "https://hooks.slack.com/",
    "https://hooks.slack.com.evil.invalid/services/T/B/X",
    "https://hooks.slack.com@evil.invalid/services/T/B/X",
    "https://hooks.slack.com/@evil.invalid/../x\\y",
    "https://hooks.slack.com/services/T/B/X extra",
    "https://hooks.slack.com:8443/services/T/B/X",
    "https://example.invalid/https://hooks.slack.com/services/T/B/X",
    "ftp://hooks.slack.com/services/T/B/X",
    "hooks.slack.com/services/T/B/X",
])
def test_only_slack_webhook_addresses_are_accepted_and_errors_do_not_echo_them(address: str) -> None:
    with pytest.raises(SlackDeliveryError) as caught:
        validated_webhook(address)
    assert address.strip() not in str(caught.value)
    assert "evil" not in str(caught.value)
    assert validated_webhook(HOOK + "\n") == HOOK


def test_deliver_error_text_omits_the_address_and_transport_detail(tmp_path: Path) -> None:
    hook = tmp_path / "deliver.webhook"
    hook.write_text(HOOK)
    config = load_config(_write(tmp_path, "deliver", slack=True, hook_file=hook)).slack

    def explode(_url, _payload, _timeout):
        raise RuntimeError(HOOK + COOKIE)

    def rejected(_url, _payload, _timeout):
        raise urllib.error.HTTPError(HOOK, 403, f"forbidden {COOKIE}", {}, None)

    for post in (explode, rejected):
        with pytest.raises(SlackDeliveryError) as caught:
            deliver(config, FileSecretResolver(), "text", post=post)
        for rendered in (str(caught.value), repr(caught.value)):
            assert "SYNTHETIC-WEBHOOK-TOKEN" not in rendered and COOKIE not in rendered
        assert caught.value.__cause__ is None and caught.value.__suppress_context__

    class Broken(FileSecretResolver):
        def read_text(self, reference):
            raise SecretReadError(HOOK)

    with pytest.raises(SlackDeliveryError) as caught:
        deliver(config, Broken(), "text", post=lambda *_args: 200)
    assert "SYNTHETIC-WEBHOOK-TOKEN" not in str(caught.value)


def test_config_repr_never_contains_the_address(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "repr", slack=True))
    for rendered in (repr(config), repr(config.slack), str(config.slack)):
        assert "SYNTHETIC-WEBHOOK-TOKEN" not in rendered and "hooks.slack.com" not in rendered
    assert not hasattr(config.slack, "webhook_url")


def test_post_webhook_posts_json_once_with_timeout_and_no_redirects(monkeypatch) -> None:
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class Opener:
        def open(self, request, timeout=None):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["body"] = request.data
            seen["type"] = request.get_header("Content-type")
            seen["timeout"] = timeout
            return Response()

    def build(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build)
    assert post_webhook(HOOK, b'{"text":"x"}', 9) == 200
    assert seen["url"] == HOOK and seen["method"] == "POST"
    assert seen["body"] == b'{"text":"x"}' and seen["timeout"] == 9
    assert seen["type"].startswith("application/json")
    (handler,) = seen["handlers"]
    assert handler().redirect_request(None, None, 302, "Found", {}, "https://evil.invalid/") is None


def test_email_and_slack_are_independent_and_a_failure_does_not_duplicate_the_other(tmp_path: Path) -> None:
    fake = FakeSlack(fail=True)
    case = _service(tmp_path, "both", [_human()], slack=True, mail=True, post=fake)
    _tick(case, local(11, 9))
    assert len(case.messages) == 1          # mail went out although Slack failed
    pending = case.storage.get_notification("joinquant-primary", "primary")
    assert pending.attempts == 1 and pending.last_error_type == "SlackDeliveryError"
    fake.fail = False
    _tick(case, local(11, 9) + timedelta(minutes=1))
    assert len(case.messages) == 1          # not re-mailed on the Slack retry
    assert len(fake.calls) == 2 and len(fake.texts) == 2
    assert case.storage.get_notification("joinquant-primary", "primary") is None

    def broken_mail(_email):
        raise RuntimeError(PASSWORD)

    other = _service(tmp_path, "mailfails", [_human()], slack=True, mail=True, connect=broken_mail)
    _tick(other, local(11, 9))
    assert len(other.slack.calls) == 1      # Slack delivered although mail failed
    assert other.storage.get_notification("joinquant-primary", "primary").last_error_type == "MailDeliveryError"
    _tick(other, local(11, 9) + timedelta(minutes=1))
    assert len(other.slack.calls) == 1      # not re-posted on the mail retry

    neither = _service(tmp_path, "bothfail", [_human()], slack=True, mail=True,
                       post=FakeSlack(fail=True), connect=broken_mail)
    _tick(neither, local(11, 9))
    pending = neither.storage.get_notification("joinquant-primary", "primary")
    assert pending.last_error_type == "ChannelDeliveryError"
    assert pending.attempts == 1


def test_mail_alone_and_slack_alone_do_not_touch_each_other(tmp_path: Path) -> None:
    only_mail = _service(tmp_path, "mailonly", [_human()], slack=False, mail=True)
    _tick(only_mail, local(11, 9))
    assert len(only_mail.messages) == 1 and only_mail.slack.calls == []
    only_slack = _service(tmp_path, "slackonly", [_human()], slack=True, mail=False)
    _tick(only_slack, local(11, 9))
    assert only_slack.messages == [] and len(only_slack.slack.calls) == 1


def test_process_events_are_not_posted_to_slack(tmp_path: Path) -> None:
    fake = FakeSlack()
    path = _write(tmp_path, "process", slack=True)
    stream = io.StringIO()
    runtime = build_runtime(path, stream=stream, slack_post=fake)
    try:
        assert isinstance(runtime.orchestrator.notifier, LifecycleNotifier)
        runtime.notifier.emit("service_started", config="synthetic", enabled_flames=0)
        runtime.notifier.emit("daemon_owner_started", owner_id="synthetic")
        runtime.notifier.emit("configuration_reload_failed", error_type="ConfigError")
        assert "service_started" in stream.getvalue()
        assert fake.calls == []
    finally:
        runtime.close()


def test_reload_adopts_slack_config_without_sending(tmp_path: Path) -> None:
    fake = FakeSlack()
    path = _write(tmp_path, "reload", slack=False)
    runtime = build_runtime(path, stream=io.StringIO(), slack_post=fake)
    try:
        assert runtime.config.slack.enabled is False
        runtime.storage.request_reload(datetime.now(timezone.utc))
        path.write_text(_toml(runtime.config.storage.path, str(tmp_path / "reload.webhook"),
                              slack=True, keeper="fake", timeout="30"))
        runtime.poll_controls()
        assert runtime.config.slack.enabled is True
        assert runtime.config.slack.timeout_seconds == 30
        assert runtime.config_view.config.slack.enabled is True
        assert runtime.storage.runtime_commands(include_finished=True)[0]["state"] == "completed"
        assert fake.calls == []
    finally:
        runtime.close()


def test_turning_slack_on_after_load_sends_the_next_lifecycle_event(tmp_path: Path, monkeypatch) -> None:
    reads = []
    current = FileSecretResolver.read_text

    def spy(self, reference):
        reads.append(reference)
        return current(self, reference)

    monkeypatch.setattr(FileSecretResolver, "read_text", spy)
    case = _service(tmp_path, "later", [_human(), _human()], slack=False)
    (tmp_path / "later.webhook").write_text(HOOK + "\n")
    _tick(case, local(11, 9))
    assert reads == [] and case.slack.calls == []
    case.view.config = load_config(_write(tmp_path, "later-on", slack=True,
                                          hook_file=tmp_path / "later.webhook"))
    _tick(case, local(11, 20))
    assert len(case.slack.calls) == 1 and "human_action_reminder" in case.slack.texts[0]
    assert reads == [str(tmp_path / "later.webhook")]
    case.view.config = load_config(_write(tmp_path, "later-off", slack=False))
    assert len(case.slack.calls) == 1


def test_slack_settings_fail_at_load_with_actionable_errors_that_do_not_echo_values(tmp_path: Path) -> None:
    hook = tmp_path / "hook.url"
    base = _toml(tmp_path / "state.sqlite3", str(hook), slack=True)
    path = tmp_path / "keepflame.toml"

    def load(text: str):
        path.write_text(text)
        return load_config(path)

    assert load(base.split("[notify.slack]", 1)[0]).slack is None
    enabled = load(base)
    assert enabled.slack.enabled is True and enabled.slack.webhook_file == hook
    assert enabled.slack.timeout_seconds == 12
    assert load(base.replace("timeout_seconds = 12\n", "")).slack.timeout_seconds == 15
    assert load(base.replace("enabled = true", "enabled = false")).slack.enabled is False
    assert load(base.replace("timeout_seconds = 12", "timeout_seconds = 1")).slack.timeout_seconds == 1
    assert load(base.replace("timeout_seconds = 12", "timeout_seconds = 120")).slack.timeout_seconds == 120

    with pytest.raises(ConfigError, match="unknown notify.slack keys: extra"):
        load(base + "extra = true\n")
    with pytest.raises(ConfigError, match="unknown notify keys: discord"):
        load(base + "\n[notify.discord]\nenabled = false\n")
    with pytest.raises(ConfigError, match="enabled must be true or false"):
        load(base.replace("enabled = true", 'enabled = "yes"'))
    for bad in ("0", "0.5", "121", "true", '"15"'):
        with pytest.raises(ConfigError, match="timeout_seconds must be a number from 1 to 120"):
            load(base.replace("timeout_seconds = 12", f"timeout_seconds = {bad}"))
    with pytest.raises(ConfigError, match="webhook_file must be an absolute path") as caught:
        load(base.replace(json.dumps(str(hook)), '"secrets/slack.url"'))
    assert "secrets/slack.url" not in str(caught.value)
    with pytest.raises(ConfigError, match="webhook_file must be a single-line absolute path"):
        load(base.replace(json.dumps(str(hook)), '""'))
    with pytest.raises(ConfigError, match="webhook_file must be a single-line absolute path"):
        load("\n".join(line for line in base.splitlines() if not line.startswith("webhook_file")))

    # A pasted address is not a path and is never echoed back.
    with pytest.raises(ConfigError, match="webhook_file must be an absolute path") as caught:
        load(base.replace(json.dumps(str(hook)), json.dumps(HOOK)))
    assert "SYNTHETIC-WEBHOOK-TOKEN" not in str(caught.value) and "hooks.slack.com" not in str(caught.value)


def test_webhook_file_inside_the_repository_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "keepflame.toml"
    inside = ROOT / "secrets" / "slack-webhook.url"
    for candidate in (inside, ROOT / "config" / "slack.url", ROOT / "x" / ".." / "slack.url"):
        path.write_text(_toml(tmp_path / "state.sqlite3", str(candidate), slack=True))
        with pytest.raises(ConfigError, match="webhook_file must be outside the repository") as caught:
            load_config(path)
        assert str(candidate) not in str(caught.value)
    link = tmp_path / "link.url"
    link.symlink_to(inside)
    path.write_text(_toml(tmp_path / "state.sqlite3", str(link), slack=True))
    with pytest.raises(ConfigError, match="outside the repository"):
        load_config(path)
    outside = ROOT.parent / "keepflame-secrets" / "slack-webhook.url"
    path.write_text(_toml(tmp_path / "state.sqlite3", str(outside), slack=True))
    assert load_config(path).slack.webhook_file == outside
    assert not outside.exists() or outside.parent != ROOT


def test_published_examples_keep_slack_off_without_a_real_address() -> None:
    examples = [
        ROOT / "config/keepflame.example.toml",
        ROOT / "config/keepflame.local.example.toml",
        ROOT / "config/keepflame.browser.example.toml",
        ROOT / "deploy/keepflame.server.toml.example",
    ]
    for path in examples:
        text = path.read_text()
        config = load_config(path)
        assert config.slack is not None and config.slack.enabled is False
        assert "hooks.slack.com" not in text
        assert "SYNTHETIC-WEBHOOK-TOKEN" not in text
        slack_block = text.split("[notify.slack]", 1)[1]
        assert "enabled = false" in slack_block
        assert config.slack.webhook_file.is_absolute()


def test_disabled_and_enabled_dry_runs_never_read_the_webhook_file(tmp_path: Path, capsys) -> None:
    from keepflame.cli import main

    for name, slack in (("dry-off", False), ("dry-on", True)):
        path = _write(tmp_path, name, slack=slack, hook_file=tmp_path / "absent.webhook", hook=None)
        assert main(["run", "--config", str(path)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["event"] == "dry_run" and payload["database_opened"] is False
        assert "hooks.slack.com" not in json.dumps(payload)
