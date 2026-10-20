"""ikuuu check-in: nav-page decoding, domain discovery, cookie handling, check-in and alerts.

All HTTP is a fake transport. The navigation page fixture is a real public copy
of https://ikuuu.bar/ fetched 2026-10-18 (obfuscated inline script, no secrets).
"""

import base64
import io
import json
import os
import stat
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.capabilities.http import HttpResponse, HttpTransportError
from keepflame.capabilities.secrets import FileSecretResolver
from keepflame.cli import main as cli_main
from keepflame.core.config import FlameConfig, ScheduleConfig
from keepflame.core.models import BusinessTarget, KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.protocols import KeeperContext
from keepflame.flames.ikuuu import IkuuuKeeper
from keepflame.flames.ikuuu.cli import import_cookie, discover
from keepflame.flames.ikuuu.cookies import CookieError, SessionCookie, load_file, parse_import, write_file
from keepflame.flames.ikuuu.discovery import (
    DomainFinder, EmailReplySource, hosts_in_page, looks_like_login, normalize_base,
)
from keepflame.flames.ikuuu.jsdecode import DecodeError, decoded_strings
from keepflame.flames.ikuuu.keeper import traffic_from_message
from keepflame.notifier.slack import render_lifecycle_message
from tests.helpers import FakeNotifier, FixedClock, MemoryStorage, StaticRegistry

NAV = (Path(__file__).parent / "fixtures" / "ikuuu" / "nav-2026-10-18.html").read_text()
ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 18, 9, 30, tzinfo=ZONE).astimezone(timezone.utc)
COOKIE = "uid=424242; email=SYNTHETIC%40example.invalid; key=SYNTHETIC-SECRET-KEY; expire_in=1790000000"


def login_page() -> bytes:
    inner = ('<html><head><title>登录 &mdash; iKuuu VPN</title></head><body><form action="/auth/login">'
             '<input name="email"><input name="password" type="password"></form></body></html>')
    encoded = base64.b64encode(inner.encode()).decode()
    return f'<html><head><script>var originBody = "{encoded}";document.write(atob(originBody));</script></head></html>'.encode()


class FakeHttp:
    """Routes (method, url) to responses; anything unrouted is a transport error."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []

    def send(self, request, *, timeout_seconds):
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url))
        if handler is None:
            raise HttpTransportError("unrouted synthetic request")
        if callable(handler):
            return handler(request)
        return handler


def ok(url, body, status=200, headers=None):
    return HttpResponse(status, url, {k.casefold(): v for k, v in (headers or {}).items()}, body)


def nav_routes(*, login_hosts=("ikuuu.pw",), nav_hosts=("ikuuu.top",)):
    routes = {("GET", "https://ikuuu.bar/"): ok("https://ikuuu.bar/", NAV.encode())}
    for host in login_hosts:
        routes[("GET", f"https://{host}/auth/login")] = ok(f"https://{host}/auth/login", login_page())
    for host in nav_hosts:  # nav mirrors answer every path with the nav page
        routes[("GET", f"https://{host}/auth/login")] = ok(f"https://{host}/auth/login", NAV.encode())
    return routes


def checkin_json(payload, status=200):
    return lambda request: ok(request.url, json.dumps(payload, ensure_ascii=False).encode(), status)


# --- nav page decoding -------------------------------------------------------------

def test_real_nav_page_obfuscated_script_yields_the_login_domains():
    hosts, methods = hosts_in_page(NAV)
    assert hosts[:2] == ["ikuuu.top", "ikuuu.pw"]
    assert methods[0] == "obfuscated_script"
    script = NAV.split("<script>")[1].split("</script>")[0]
    values = decoded_strings(script)
    assert "https://ikuuu.top/" in values and "https://ikuuu.pw/" in values


@pytest.mark.parametrize("script", [
    "console.log('ikuuu.top')",
    "function _0xabc(){const _0x1=['a'];_0xabc=function(){return _0x1;};return _0xabc();}",
    "x" * 1_000_001,
])
def test_unsupported_scripts_are_rejected_without_execution(script):
    with pytest.raises(DecodeError):
        decoded_strings(script)


def test_plain_text_hosts_are_a_fallback_when_no_script_decodes():
    hosts, methods = hosts_in_page("<p>最新地址 ikuuu.new 与 ikuuu.alt</p><script>var a=1</script>")
    assert hosts == ["ikuuu.new", "ikuuu.alt"] and methods == ["plain_text"]


def test_login_form_detection():
    assert looks_like_login(login_page())
    assert looks_like_login(b'<form action="/auth/login"><input name="email"><input name="passwd"></form>')
    assert not looks_like_login(NAV.encode())
    assert not looks_like_login(b'<script>var originBody = "!!!!notbase64!!!!";</script>')


@pytest.mark.parametrize("value, expected", [
    ("ikuuu.pw", "https://ikuuu.pw"), ("https://IKUUU.top/auth/login?x=1", "https://ikuuu.top"),
    ("http://ikuuu.pw", None), ("https://ikuuu.pw:8443", None), ("https://u:p@ikuuu.pw", None),
    ("https://evil.example", None), ("https://ikuuu.pw.evil.example", None), ("", None),
])
def test_base_normalization_only_accepts_allowed_https_hosts(value, expected):
    assert normalize_base(value) == expected


# --- discovery ---------------------------------------------------------------------

def test_discovery_skips_nav_mirrors_and_returns_the_first_real_login_domain():
    http = FakeHttp(nav_routes())
    pauses = []
    found = DomainFinder(http, sleeper=pauses.append).discover()
    assert found.base_url == "https://ikuuu.pw"
    assert found.candidates == ("https://ikuuu.top", "https://ikuuu.pw")
    assert found.probed == ("https://ikuuu.top", "https://ikuuu.pw")
    assert all(request.method == "GET" for request in http.requests)
    assert pauses == [1.0, 1.0]  # paced between requests


def test_known_base_is_probed_first_and_avoids_the_nav_page():
    http = FakeHttp(nav_routes())
    found = DomainFinder(http, sleeper=lambda _s: None).discover(["https://ikuuu.pw"])
    assert found.base_url == "https://ikuuu.pw" and len(http.requests) == 1


def test_probe_rejects_a_login_page_served_from_another_host():
    routes = nav_routes(login_hosts=())
    routes[("GET", "https://ikuuu.pw/auth/login")] = ok("https://phish.example/auth/login", login_page())
    found = DomainFinder(FakeHttp(routes), sleeper=lambda _s: None).discover()
    assert found.base_url is None


def test_probe_budget_and_unreachable_seeds():
    found = DomainFinder(FakeHttp({}), sleeper=lambda _s: None).discover()
    assert found.base_url is None and found.seeds_reached == 0
    budget = DomainFinder(FakeHttp(nav_routes(login_hosts=(), nav_hosts=("ikuuu.top", "ikuuu.pw"))),
                          sleeper=lambda _s: None, max_probes=1).discover()
    assert budget.probed == ("https://ikuuu.top",)


def test_email_reply_hook_feeds_candidates_without_sending_mail():
    routes = {("GET", "https://ikuuu.mail/auth/login"): ok("https://ikuuu.mail/auth/login", login_page())}
    source = EmailReplySource(lambda: "您好，当前可用地址：https://ikuuu.mail 请收藏。")
    found = DomainFinder(FakeHttp(routes), sleeper=lambda _s: None, extra_sources=[source]).discover()
    assert found.base_url == "https://ikuuu.mail" and "email_reply" in found.methods
    assert EmailReplySource().candidates() == ()


# --- cookies -----------------------------------------------------------------------

def test_cookie_import_formats():
    assert parse_import("Cookie: a=1; b=2").header == "a=1; b=2"
    exported = json.dumps([{"name": "uid", "value": "1", "domain": ".ikuuu.pw"},
                           {"name": "key", "value": "x", "domain": "ikuuu.pw"}])
    parsed = parse_import(exported)
    assert parsed.header == "uid=1; key=x" and parsed.base_url == "https://ikuuu.pw"
    for bad in ("", "a=1\nb=2", "novalue", "a=1; b", "[1]", "{", "x" * 9000):
        with pytest.raises(CookieError) as error:
            parse_import(bad)
        assert "a=1" not in str(error.value)


def test_cookie_file_is_written_atomically_private_and_round_trips(tmp_path):
    path = tmp_path / "ikuuu.cookie"
    path.write_text("old")
    path.chmod(0o644)
    write_file(path, SessionCookie(COOKIE, "https://ikuuu.pw"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = load_file(FileSecretResolver().read_text(str(path)))
    assert loaded == SessionCookie(COOKIE, "https://ikuuu.pw")
    assert [p.name for p in tmp_path.iterdir()] == ["ikuuu.cookie"]
    link = tmp_path / "link.cookie"
    link.symlink_to(path)
    with pytest.raises(CookieError):
        write_file(link, loaded)
    with pytest.raises(CookieError):
        load_file('{"cookie": "a=1", "base_url": "https://evil.example"}')


# --- keeper ------------------------------------------------------------------------

def flame(tmp_path, **settings) -> FlameConfig:
    values = {"cookie_file": str(tmp_path / "ikuuu.cookie")}
    values.update(settings)
    return FlameConfig("ikuuu-primary", "ikuuu", "primary", True, ScheduleConfig(
        "Asia/Shanghai", time(8, 30), 60, time(20), time(23), first_window_end=time(10, 30)), values)


def store_cookie(tmp_path, base="https://ikuuu.pw", header=COOKIE):
    write_file(tmp_path / "ikuuu.cookie", SessionCookie(header, base))


def run(tmp_path, http, *, storage=None, notifier=None, config=None, now=NOW):
    storage = storage or MemoryStorage()
    notifier = notifier or FakeNotifier()
    runner = Orchestrator(storage=storage, registry=StaticRegistry(IkuuuKeeper(sleeper=lambda _s: None)),
                          capabilities=CapabilityRegistry({"http": http}), secrets=FileSecretResolver(),
                          notifier=notifier, clock=FixedClock(now))
    outcome = runner.run_flame(config or flame(tmp_path), now=now)
    return outcome, storage, notifier


def dumped(outcome, notifier) -> str:
    return json.dumps([outcome.record.result.metadata, outcome.record.result.message,
                       [str(e) for e in notifier.events]], default=str, ensure_ascii=False)


def test_checkin_success_records_traffic_and_sends_cookie_only_to_its_domain(tmp_path):
    store_cookie(tmp_path)
    routes = nav_routes()
    routes[("POST", "https://ikuuu.pw/user/checkin")] = checkin_json({"ret": 1, "msg": "你获得了 512MB 流量"})
    http = FakeHttp(routes)
    outcome, _, notifier = run(tmp_path, http)
    result = outcome.record.result
    assert result.status is KeepStatus.SUCCESS
    assert result.metadata["traffic_gained"] == "512MB" and result.metadata["traffic_gained_mb"] == 512.0
    assert result.metadata["checkin_state"] == "checked_in" and result.metadata["base_url"] == "https://ikuuu.pw"
    post = [r for r in http.requests if r.method == "POST"]
    assert len(post) == 1 and post[0].headers["Cookie"] == COOKIE
    assert all("Cookie" not in r.headers for r in http.requests if r.method == "GET")
    assert "SYNTHETIC-SECRET-KEY" not in dumped(outcome, notifier)
    assert outcome.state.next_action_at.astimezone(ZONE).date().isoformat() == "2026-10-19"


@pytest.mark.parametrize("message", ["您似乎已经签到过了...", "You have already checked in today"])
def test_already_checked_in_counts_as_success(tmp_path, message):
    store_cookie(tmp_path)
    routes = nav_routes()
    routes[("POST", "https://ikuuu.pw/user/checkin")] = checkin_json({"ret": 0, "msg": message})
    outcome, _, _ = run(tmp_path, FakeHttp(routes))
    assert outcome.record.result.status is KeepStatus.SUCCESS
    assert outcome.record.result.metadata["checkin_state"] == "already_checked_in"


@pytest.mark.parametrize("response", [
    lambda r: ok(r.url, b"", 302, {"Location": "/auth/login"}),
    lambda r: ok(r.url, b"", 403),
    lambda r: ok(r.url, login_page()),
    checkin_json({"ret": -1, "msg": "请登录后再操作"}),
])
def test_expired_session_waits_for_a_human_with_login_url_and_import_command(tmp_path, response):
    store_cookie(tmp_path)
    routes = nav_routes()
    routes[("POST", "https://ikuuu.pw/user/checkin")] = response
    outcome, _, notifier = run(tmp_path, FakeHttp(routes))
    result = outcome.record.result
    assert result.status is KeepStatus.WAITING_HUMAN and result.metadata["reason"] == "session_expired"
    assert result.human_action.url == "https://ikuuu.pw/auth/login"
    assert "keepflame ikuuu import-cookie --flame ikuuu-primary --base https://ikuuu.pw" in result.human_action.instructions
    assert [e["event"] for e in notifier.events] == ["human_action_required"]  # immediate alert
    text = render_lifecycle_message(event="human_action_required", notification_id="n", flame_id="ikuuu-primary",
                                    account_id="primary", result=result)
    assert "Action-URL: https://ikuuu.pw/auth/login" in text and "import-cookie" in text
    assert "SYNTHETIC-SECRET-KEY" not in text + dumped(outcome, notifier)


def test_domain_change_alerts_and_never_sends_the_old_cookie_to_the_new_domain(tmp_path):
    store_cookie(tmp_path, base="https://ikuuu.top")  # top now only mirrors the nav page
    http = FakeHttp(nav_routes())
    outcome, _, notifier = run(tmp_path, http)
    result = outcome.record.result
    assert result.status is KeepStatus.WAITING_HUMAN and result.metadata["reason"] == "domain_changed"
    assert result.metadata["imported_for_base_url"] == "https://ikuuu.top"
    assert result.human_action.url == "https://ikuuu.pw/auth/login"
    assert not any(r.method == "POST" for r in http.requests)
    assert all("Cookie" not in r.headers for r in http.requests)
    assert [e["event"] for e in notifier.events] == ["human_action_required"]


def test_wrong_manual_base_url_alerts_with_the_discovered_login_url(tmp_path):
    write_file(tmp_path / "ikuuu.cookie", SessionCookie(COOKIE, None))
    config = flame(tmp_path, base_url="https://ikuuu.wrong")
    http = FakeHttp(nav_routes())
    outcome, _, notifier = run(tmp_path, http, config=config)
    result = outcome.record.result
    assert result.metadata["reason"] == "domain_changed"
    assert result.human_action.url == "https://ikuuu.pw/auth/login"
    assert notifier.events and not any(r.method == "POST" for r in http.requests)


def test_manual_base_url_works_when_the_nav_page_is_down(tmp_path):
    write_file(tmp_path / "ikuuu.cookie", SessionCookie(COOKIE, None))
    routes = {("GET", "https://ikuuu.pw/auth/login"): ok("https://ikuuu.pw/auth/login", login_page()),
              ("POST", "https://ikuuu.pw/user/checkin"): checkin_json({"ret": 1, "msg": "获得 1GB 流量"})}
    outcome, _, _ = run(tmp_path, FakeHttp(routes), config=flame(tmp_path, base_url="https://ikuuu.pw"))
    assert outcome.record.result.status is KeepStatus.SUCCESS
    assert outcome.record.result.metadata["traffic_gained_mb"] == 1024.0


@pytest.mark.parametrize("prepare, reason", [
    (lambda p: None, "cookie_unreadable"),
    (lambda p: (p.write_text(json.dumps({"cookie": COOKIE, "base_url": "https://ikuuu.pw"})), p.chmod(0o644)), "cookie_unreadable"),
    (lambda p: (p.write_text("not a cookie"), p.chmod(0o600)), "cookie_invalid"),
    (lambda p: (p.write_text(COOKIE), p.chmod(0o600)), "cookie_unbound"),
])
def test_cookie_problems_wait_for_a_human_on_the_current_domain(tmp_path, prepare, reason):
    prepare(tmp_path / "ikuuu.cookie")
    http = FakeHttp(nav_routes())
    outcome, _, notifier = run(tmp_path, http)
    result = outcome.record.result
    assert result.status is KeepStatus.WAITING_HUMAN and result.metadata["reason"] == reason
    assert result.human_action.url == "https://ikuuu.pw/auth/login"
    assert not any(r.method == "POST" for r in http.requests)


def test_discovery_failure_uses_bounded_retries_then_alerts_the_same_day(tmp_path):
    store_cookie(tmp_path)
    storage, notifier = MemoryStorage(), FakeNotifier()
    for minutes in (0, 30, 90, 210):
        now = NOW.replace(minute=0) + timedelta(minutes=30 + minutes)
        outcome, storage, notifier = run(tmp_path, FakeHttp({}), storage=storage, notifier=notifier, now=now)
        assert outcome.record.result.metadata["reason"] == "domain_discovery_failed"
        assert "automation_paused" not in outcome.record.result.metadata
    assert [e["event"] for e in notifier.events] == ["retries_exhausted"]


def test_rate_limit_and_transport_errors_are_retryable_failures(tmp_path):
    store_cookie(tmp_path)
    routes = nav_routes()
    routes[("POST", "https://ikuuu.pw/user/checkin")] = lambda r: ok(r.url, b"busy", 503, {"Retry-After": "7200"})
    outcome, _, _ = run(tmp_path, FakeHttp(routes))
    assert outcome.record.result.metadata["reason"] == "checkin_http_error"
    assert outcome.state.next_action_at >= NOW + timedelta(hours=2)
    del routes[("POST", "https://ikuuu.pw/user/checkin")]
    outcome, _, _ = run(tmp_path, FakeHttp(routes))
    assert outcome.record.result.metadata["reason"] == "checkin_transport_error"


def test_unchanged_cookie_while_waiting_makes_no_requests_until_reimport(tmp_path):
    store_cookie(tmp_path)
    routes = nav_routes()
    routes[("POST", "https://ikuuu.pw/user/checkin")] = lambda r: ok(r.url, b"", 302, {"Location": "/auth/login"})
    storage = MemoryStorage()
    first, storage, notifier = run(tmp_path, FakeHttp(routes), storage=storage)
    quiet = FakeHttp(routes)
    later = NOW + timedelta(hours=1)
    second, storage, notifier = run(tmp_path, quiet, storage=storage, notifier=notifier, now=later)
    assert quiet.requests == [] and second.record.result.metadata["network"] == "skipped_unchanged_import"
    assert second.record.result.human_action == first.record.result.human_action
    # Re-import a fresh cookie: the next recheck signs in again.
    store_cookie(tmp_path, header="uid=424242; key=SYNTHETIC-FRESH-KEY")
    routes[("POST", "https://ikuuu.pw/user/checkin")] = checkin_json({"ret": 1, "msg": "你获得了 300MB 流量"})
    third, _, notifier = run(tmp_path, FakeHttp(routes), storage=storage, notifier=notifier,
                             now=later + timedelta(hours=1))
    assert third.record.result.status is KeepStatus.SUCCESS
    assert "flame_recovered" in [e["event"] for e in notifier.events]


def test_invalid_settings_pause_instead_of_guessing(tmp_path):
    outcome, _, _ = run(tmp_path, FakeHttp({}), config=flame(tmp_path, cookie_file="relative.cookie"))
    assert outcome.record.result.status is KeepStatus.FAILED
    assert outcome.record.result.metadata["automation_paused"] is True


def test_traffic_parsing():
    assert traffic_from_message("你获得了 1.5 GB 流量") == ("1.5GB", 1536.0)
    assert traffic_from_message("签到成功") == (None, None)


# --- CLI ---------------------------------------------------------------------------

CONFIG = """
[storage]
path = "{tmp}/state.sqlite3"

[[flames]]
id = "ikuuu-primary"
keeper = "ikuuu"
account_id = "primary"
enabled = true

[flames.schedule]
timezone = "Asia/Shanghai"
first_window_start = "08:30"
first_window_end = "10:30"
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"

[flames.settings]
cookie_file = "{tmp}/ikuuu.cookie"
nav_seeds = ["https://ikuuu.bar/"]
"""


def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(tmp=tmp_path))
    return path


class Pipe(io.StringIO):
    def isatty(self):
        return False


def test_import_cookie_command_writes_a_private_bound_file_and_prints_no_values(tmp_path):
    out = io.StringIO()
    args = SimpleNamespace(config=config_file(tmp_path), flame=None, base="https://ikuuu.pw", from_clipboard=False)
    assert import_cookie(args, stdin=Pipe(COOKIE + "\n"), out=out) == 0
    record = json.loads(out.getvalue())
    assert record["base_url"] == "https://ikuuu.pw" and record["cookie_names"] == ["uid", "email", "key", "expire_in"]
    assert "SYNTHETIC" not in out.getvalue()
    path = tmp_path / "ikuuu.cookie"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_file(path.read_text()) == SessionCookie(COOKIE, "https://ikuuu.pw")


def test_import_cookie_requires_an_allowed_base(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", Pipe(COOKIE))
    code = cli_main(["ikuuu", "import-cookie", "--config", str(config_file(tmp_path)), "--base", "https://evil.example"])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code != 0 and "CookieError" in output and "SYNTHETIC" not in output
    monkeypatch.setattr("sys.stdin", Pipe(COOKIE))
    assert cli_main(["ikuuu", "import-cookie", "--config", str(config_file(tmp_path))]) != 0
    assert not (tmp_path / "ikuuu.cookie").exists()


def test_discover_command_reports_candidates(tmp_path):
    out = io.StringIO()
    args = SimpleNamespace(config=config_file(tmp_path), flame="ikuuu-primary")
    assert discover(args, http=FakeHttp(nav_routes()), out=out) == 0
    record = json.loads(out.getvalue())
    assert record["base_url"] == "https://ikuuu.pw" and record["candidates"] == ["https://ikuuu.top", "https://ikuuu.pw"]


def test_example_config_loads_and_settings_validate():
    from keepflame.core.config import load_config
    from keepflame.flames.ikuuu.keeper import _Settings

    path = Path(__file__).resolve().parents[1] / "config" / "keepflame.ikuuu.example.toml"
    config = load_config(path)
    (flame_config,) = config.flames
    assert flame_config.keeper == "ikuuu" and flame_config.enabled is False
    settings = _Settings(flame_config.settings)
    assert settings.seeds == ("https://ikuuu.bar/",) and settings.base_url is None
    assert config.slack is None or config.slack.enabled is False


def test_rfc6265_quoted_cookie_value_is_accepted_and_kept_verbatim(tmp_path):
    quoted = 'uid="SYNTHETIC|quoted=="; key=plain'
    assert parse_import(quoted).header == quoted
    exported = json.dumps([{"name": "uid", "value": '"SYNTHETIC|quoted=="', "domain": "ikuuu.pw"},
                           {"name": "key", "value": "plain", "domain": "ikuuu.pw"}])
    parsed = parse_import(exported)
    assert parsed.header == quoted and parsed.base_url == "https://ikuuu.pw"
    path = tmp_path / "ikuuu.cookie"
    write_file(path, SessionCookie(quoted, "https://ikuuu.pw"))
    assert load_file(FileSecretResolver().read_text(str(path))).header == quoted
    for bad in ('uid="abc', 'uid=abc"', 'uid="a"b"', 'uid=a"b', 'uid="a,b"', 'uid="'):
        with pytest.raises(CookieError):
            parse_import(bad)
