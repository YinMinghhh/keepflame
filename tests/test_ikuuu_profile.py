"""ikuuu dedicated-profile session: scripted panels only.

Nothing here starts Chrome or contacts ikuuu. A file that remembers "accepted"
is a stand-in for the profile directory, not evidence that Chrome restored a
login or that a real account can run unattended.
"""

from __future__ import annotations

import json
import stat
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.capabilities.browser import ProfileNotAuthorized
from keepflame.capabilities.http import HttpResponse
from keepflame.capabilities.profile_owner import ProfileInUse, exclusive_profile
from keepflame.capabilities.secrets import FileSecretResolver
from keepflame.cli import main as cli_main
from keepflame.core.models import KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.flames.ikuuu import IkuuuKeeper
from keepflame.flames.ikuuu.panel import PanelError, open_panel
from keepflame.capabilities.browser import everyday_chrome_profile, validate_profile
from keepflame.flames.ikuuu.session import challenge_markers, extra_hosts
from tests.helpers import FakeNotifier, FixedClock, MemoryStorage, StaticRegistry
from tests.test_ikuuu import NOW, ZONE, FakeHttp, flame, nav_routes

def enroll(tmp_path) -> Path:
    parent = tmp_path / "profiles"
    parent.mkdir(mode=0o700)
    profile = parent / "ikuuu"
    profile.mkdir(mode=0o700)
    marker = profile / ".keepflame-profile.json"
    marker.write_text(json.dumps({"version": 1, "account_id": "primary", "purpose": "keepflame"}, separators=(",", ":")))
    marker.chmod(0o600)
    return profile


def profile_config(tmp_path, profile, **extra):
    values = {"cookie_file": None, "browser_profile_dir": str(profile), "base_url": "https://ikuuu.pw"}
    values.update(extra)
    return flame(tmp_path, **values)


def response(status, url, body, **headers):
    return HttpResponse(status, url, {key.casefold(): value for key, value in headers.items()}, body)


def plain_login():
    html = (b"<form action='/auth/login'><input name='email'><input name='password' type='password'>"
            b"<input name='remember' type='checkbox'></form>")
    return response(200, "https://ikuuu.pw/auth/login", html)


def challenge_login():
    html = ('<form action="/auth/login"><input name="email"><input name="password"><script src="/gt4.js"></script>'
            '<script src="https://small-files.example/jquery.js"></script>'
            '<div id="2fa-form">二步验证</div>登录</form>').encode()
    return response(200, "https://ikuuu.pw/auth/login", html)


def checkin(ok, message="10MB"):
    if ok:
        return response(200, "https://ikuuu.pw/user/checkin", json.dumps({"ret": 1, "msg": message}).encode())
    return response(302, "https://ikuuu.pw/user/checkin", b"", location="/auth/login")


class Scripted:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.bodies = []

    def request(self, method, url, *, headers, body=b""):
        self.calls.append((method, url))
        self.bodies.append(body)
        assert "cookie" not in {key.lower() for key in headers}
        item = self.steps.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def opener_for(panel):
    @contextmanager
    def opener(*, profile_dir, account_id, origin, allowed_hosts=(), headless=True, login_window=False):
        assert headless is True and allowed_hosts == () and login_window is False
        assert origin == "https://ikuuu.pw"
        yield panel

    return opener


def run(tmp_path, panel, *, config, storage=None, notifier=None, now=NOW, http=None):
    storage = storage or MemoryStorage()
    notifier = notifier or FakeNotifier()
    keeper = IkuuuKeeper(sleeper=lambda _s: None, panel_opener=opener_for(panel))
    runner = Orchestrator(
        storage=storage, registry=StaticRegistry(keeper),
        capabilities=CapabilityRegistry({"http": http or FakeHttp(nav_routes())}),
        secrets=FileSecretResolver(), notifier=notifier, clock=FixedClock(now),
    )
    return runner.run_flame(config, now=now), storage, notifier


def dumped(outcome, notifier) -> str:
    return json.dumps([outcome.record.result.metadata, outcome.record.result.message,
                       [str(event) for event in notifier.events]], ensure_ascii=False)


def test_challenge_markers_and_asset_hosts_stay_on_the_fixed_list():
    markers = challenge_markers(challenge_login().body)
    assert markers == ("geetest", "mfa")
    hosts = extra_hosts(challenge_login().body)
    assert "small-files.example" in hosts and "static.geetest.com" in hosts
    assert extra_hosts(plain_login().body) == ()
    # The originBody wrapper matches, but the payload is not valid base64.
    broken = b'<script>originBody = "AAAAAAAAAAAAA===";</script>'
    assert challenge_markers(broken) == ("unreadable_document",)


def test_valid_profile_session_checks_in_without_reading_a_cookie_file(tmp_path):
    profile = enroll(tmp_path)
    panel = Scripted([checkin(True, "512MB")])
    outcome, _, notifier = run(tmp_path, panel, config=profile_config(tmp_path, profile))
    result = outcome.record.result
    assert result.status is KeepStatus.SUCCESS and result.metadata["channel"] == "profile"
    assert result.metadata["traffic_gained"] == "512MB"
    assert panel.calls == [("POST", "https://ikuuu.pw/user/checkin")]
    assert notifier.events == []


def test_geetest_document_stops_and_does_not_post_credentials(tmp_path):
    profile = enroll(tmp_path)
    panel = Scripted([checkin(False), challenge_login()])
    outcome, _, notifier = run(tmp_path, panel, config=profile_config(tmp_path, profile))
    result = outcome.record.result
    assert result.status is KeepStatus.WAITING_HUMAN and result.metadata["reason"] == "challenge_required"
    assert result.metadata["renewal"] == "not_attempted"
    assert result.metadata["challenge_markers"] == ["geetest", "mfa"]
    assert panel.calls == [("POST", "https://ikuuu.pw/user/checkin"), ("GET", "https://ikuuu.pw/auth/login")]
    assert all(body == b"" for body in panel.bodies)
    assert "ikuuu login" in result.human_action.instructions and "--execute" in result.human_action.instructions
    assert [event["event"] for event in notifier.events] == ["human_action_required"]


def test_missing_challenge_markers_do_not_become_a_login(tmp_path):
    profile = enroll(tmp_path)
    panel = Scripted([checkin(False), plain_login()])
    outcome, _, notifier = run(tmp_path, panel, config=profile_config(tmp_path, profile))
    result = outcome.record.result
    assert result.metadata["reason"] == "session_expired"
    assert result.metadata["renewal"] == "not_attempted"
    assert result.metadata["challenge_markers"] == []
    assert result.metadata["evidence"] == "no_challenge_marker"
    assert panel.calls == [("POST", "https://ikuuu.pw/user/checkin"), ("GET", "https://ikuuu.pw/auth/login")]
    assert not any(url.endswith("/auth/login") and method == "POST" for method, url in panel.calls)
    assert "password" not in dumped(outcome, notifier)


def test_unreadable_login_document_fails_closed(tmp_path):
    profile = enroll(tmp_path)
    broken = response(200, "https://ikuuu.pw/auth/login", b'<script>originBody = "AAAAAAAAAAAAA===";</script>')
    panel = Scripted([checkin(False), broken])
    outcome, _, _ = run(tmp_path, panel, config=profile_config(tmp_path, profile))
    assert outcome.record.result.metadata["reason"] == "challenge_required"
    assert outcome.record.result.metadata["challenge_markers"] == ["unreadable_document"]
    assert panel.calls[-1][0] == "GET"


def test_credential_file_setting_is_rejected(tmp_path):
    profile = enroll(tmp_path)
    outcome, _, _ = run(
        tmp_path, Scripted([]), config=profile_config(tmp_path, profile, credential_file="/tmp/not-used"))
    assert outcome.record.result.metadata["automation_paused"] is True
    assert outcome.record.result.metadata["setting"] == "credential_file"


def test_missing_credentials_ask_for_the_login_window(tmp_path):
    profile = enroll(tmp_path)
    panel = Scripted([checkin(False), plain_login()])
    outcome, _, _ = run(tmp_path, panel, config=profile_config(tmp_path, profile))
    result = outcome.record.result
    assert result.metadata["reason"] == "session_expired"
    assert "-m keepflame ikuuu login" in result.human_action.instructions
    assert panel.calls[-1][0] == "GET"


def test_profile_file_remembers_acceptance_for_a_later_opener(tmp_path):
    """A file written between openers stands in for the profile. Not Chrome and not ikuuu.

    The keeper does not create that file. A human login outside this process does.
    """
    profile = enroll(tmp_path)
    state = tmp_path / "accepted"
    config = profile_config(tmp_path, profile)

    class Disk:
        def __init__(self):
            self.calls = []

        def request(self, method, url, *, headers, body=b""):
            self.calls.append((method, url))
            if method == "POST" and url.endswith("/auth/login"):
                raise AssertionError("profile renewal must not post the login form")
            if method == "POST" and url.endswith("/user/checkin"):
                if state.exists():
                    return checkin(True, "20MB")
                return checkin(False)
            return plain_login()

    first = Disk()
    outcome, storage, notifier = run(tmp_path, first, config=config)
    assert outcome.record.result.metadata["renewal"] == "not_attempted" and not state.exists()
    state.write_text("accepted")
    second = Disk()
    outcome, _, notifier = run(tmp_path, second, config=config, storage=storage, notifier=notifier,
                               now=NOW + timedelta(days=1))
    assert outcome.record.result.status is KeepStatus.SUCCESS
    assert second.calls == [("POST", "https://ikuuu.pw/user/checkin")]
    assert "password" not in dumped(outcome, notifier)


def test_human_login_between_runs_is_a_recovery_not_another_waiting_alert(tmp_path):
    profile = enroll(tmp_path)
    config = profile_config(tmp_path, profile)
    storage, notifier = MemoryStorage(), FakeNotifier()
    first, storage, notifier = run(tmp_path, Scripted([checkin(False), challenge_login()]),
                                   config=config, storage=storage, notifier=notifier)
    assert first.record.result.metadata["reason"] == "challenge_required"
    second, _, notifier = run(tmp_path, Scripted([checkin(True, "144MB")]), config=config,
                              storage=storage, notifier=notifier, now=NOW + timedelta(hours=1))
    assert second.record.result.status is KeepStatus.SUCCESS
    assert [event["event"] for event in notifier.events] == ["human_action_required", "flame_recovered"]


def test_profile_problems_are_classified_without_a_traceback_secret(tmp_path):
    profile = enroll(tmp_path)
    config = profile_config(tmp_path, profile)

    @contextmanager
    def missing(**kwargs):
        raise ProfileNotAuthorized("synthetic enrollment")
        yield

    keeper = IkuuuKeeper(sleeper=lambda _s: None, panel_opener=missing)
    runner = Orchestrator(
        storage=MemoryStorage(), registry=StaticRegistry(keeper),
        capabilities=CapabilityRegistry({"http": FakeHttp(nav_routes())}),
        secrets=FileSecretResolver(), notifier=FakeNotifier(), clock=FixedClock(NOW),
    )
    outcome = runner.run_flame(config, now=NOW)
    assert outcome.record.result.metadata["reason"] == "profile_not_enrolled"

    @contextmanager
    def busy(**kwargs):
        raise ProfileInUse("profile is already owned")
        yield

    runner = Orchestrator(
        storage=MemoryStorage(), registry=StaticRegistry(IkuuuKeeper(sleeper=lambda _s: None, panel_opener=busy)),
        capabilities=CapabilityRegistry({"http": FakeHttp(nav_routes())}),
        secrets=FileSecretResolver(), notifier=FakeNotifier(), clock=FixedClock(NOW),
    )
    outcome = runner.run_flame(config, now=NOW)
    assert outcome.record.result.metadata["reason"] == "browser_busy"

    @contextmanager
    def unavailable(**kwargs):
        raise PanelError("browser_missing")
        yield

    runner = Orchestrator(
        storage=MemoryStorage(),
        registry=StaticRegistry(IkuuuKeeper(sleeper=lambda _s: None, panel_opener=unavailable)),
        capabilities=CapabilityRegistry({"http": FakeHttp(nav_routes())}),
        secrets=FileSecretResolver(), notifier=FakeNotifier(), clock=FixedClock(NOW),
    )
    outcome = runner.run_flame(config, now=NOW)
    assert outcome.record.result.status is KeepStatus.FAILED
    assert outcome.record.result.metadata["reason"] == "browser_unavailable"
    assert outcome.record.result.metadata["error_type"] == "browser_missing"


def test_configured_origin_is_not_abandoned_for_a_discovered_host(tmp_path):
    profile = enroll(tmp_path)
    opened = []

    @contextmanager
    def opener(**kwargs):
        opened.append(kwargs["origin"])
        yield Scripted([])

    keeper = IkuuuKeeper(sleeper=lambda _s: None, panel_opener=opener)
    runner = Orchestrator(
        storage=MemoryStorage(), registry=StaticRegistry(keeper),
        capabilities=CapabilityRegistry({"http": FakeHttp(nav_routes())}),
        secrets=FileSecretResolver(), notifier=FakeNotifier(), clock=FixedClock(NOW),
    )
    # ikuuu.top is the nav mirror in the fixture; discovery then selects ikuuu.pw.
    config = profile_config(tmp_path, profile, base_url="https://ikuuu.top")
    outcome = runner.run_flame(config, now=NOW)
    assert outcome.record.result.metadata["reason"] == "domain_changed"
    assert outcome.record.result.metadata["configured_base_url"] == "https://ikuuu.top"
    assert opened == []


def test_profile_without_base_url_is_invalid(tmp_path):
    profile = enroll(tmp_path)
    outcome, _, _ = run(tmp_path, Scripted([]), config=profile_config(tmp_path, profile, base_url=None))
    assert outcome.record.result.metadata["automation_paused"] is True
    assert outcome.record.result.metadata["setting"] == "base_url"


def test_profile_lock_rejects_a_second_owner(tmp_path):
    profile = enroll(tmp_path)
    with exclusive_profile(str(profile)):
        with pytest.raises(ProfileInUse):
            with exclusive_profile(str(profile)):
                pass


def test_open_panel_relaunches_the_same_directory_and_hides_set_cookie(monkeypatch, tmp_path):
    profile = enroll(tmp_path)
    launches = []
    routes = []

    class Response:
        status = 302
        headers = {"Location": "/auth/login?ticket=SYNTHETIC", "Set-Cookie": "key=SYNTHETIC-SECRET"}

        def body(self):
            return b""

        def dispose(self):
            pass

    class Context:
        def __init__(self):
            self.request = SimpleNamespace(fetch=self.fetch)

        def fetch(self, url, **kwargs):
            assert "cookie" not in {key.lower() for key in kwargs.get("headers", {})}
            return Response()

        def close(self):
            pass

        def route(self, _pattern, handler):
            routes.append(handler)

        def route_web_socket(self, *_args):
            pass

        def new_page(self):
            return SimpleNamespace(goto=lambda *_a, **_k: None, wait_for_event=lambda *_a, **_k: None)

    class Chromium:
        def launch_persistent_context(self, directory, **kwargs):
            launches.append((directory, kwargs["headless"]))
            return Context()

    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=Chromium())

    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=playwright))
    for _ in range(2):
        with open_panel(profile_dir=str(profile), account_id="primary", origin="https://ikuuu.pw") as panel:
            got = panel.request("POST", "https://ikuuu.pw/user/checkin", headers={"Cookie": "drop-me"}, body=b"")
            assert got.header("location") == "/auth/login?ticket=SYNTHETIC"
            assert "set-cookie" not in got.headers
            with pytest.raises(PanelError) as raised:
                panel.request("GET", "https://evil.example/", headers={})
            assert raised.value.reason == "off_origin"
    assert launches == [(str(profile), True), (str(profile), True)]

    with open_panel(profile_dir=str(profile), account_id="primary", origin="https://ikuuu.pw",
                    allowed_hosts=("static.geetest.com",), login_window=True, headless=False) as window:
        route = routes[-1]

        class FakeRoute:
            def __init__(self, url, method="GET"):
                self.request = SimpleNamespace(url=url, method=method)
                self.aborted = None
                self.fulfilled = False

            def abort(self, reason):
                self.aborted = reason

            def fetch(self, **_kwargs):
                return Response()

            def fulfill(self, **_kwargs):
                self.fulfilled = True

        blocked = FakeRoute("https://evil.example/x")
        route(blocked)
        assert blocked.aborted == "blockedbyclient"
        posted = FakeRoute("https://ikuuu.pw/user/checkin", "POST")
        route(posted)
        assert posted.aborted == "blockedbyclient"
        leaked = FakeRoute("https://static.geetest.com/collect", "POST")
        route(leaked)
        assert leaked.aborted == "blockedbyclient"
        allowed = FakeRoute("https://static.geetest.com/v4.js")
        route(allowed)
        assert allowed.fulfilled is True
        window.open_login("https://ikuuu.pw/auth/login")
        assert window.wait_closed(1000) == "closed"


def test_commands_do_not_print_secrets_and_login_dry_run_stays_local(tmp_path, capsys):
    profile_parent = tmp_path / "profiles"
    profile_parent.mkdir(mode=0o700)
    profile = profile_parent / "ikuuu"
    config = tmp_path / "config.toml"
    config.write_text(f"""
[storage]
path = "{tmp_path}/state.sqlite3"
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
browser_profile_dir = "{profile}"
base_url = "https://ikuuu.pw"
nav_seeds = ["https://ikuuu.bar/"]
""")
    assert cli_main(["ikuuu", "enroll-profile", "--config", str(config), "--flame", "ikuuu-primary"]) == 0
    assert not profile.exists()
    assert cli_main(["ikuuu", "enroll-profile", "--config", str(config), "--flame", "ikuuu-primary", "--execute"]) == 0
    marker = json.loads((profile / ".keepflame-profile.json").read_text())
    assert marker == {"version": 1, "account_id": "primary", "purpose": "keepflame"}
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    repo_config = config.read_text().replace(str(profile), str(Path(__file__).resolve().parents[1] / "var" / "ikuuu"))
    rejected = tmp_path / "repo-config.toml"
    rejected.write_text(repo_config)
    assert cli_main(["ikuuu", "enroll-profile", "--config", str(rejected), "--execute"]) != 0
    from keepflame.flames.ikuuu.cli import login_window
    from types import SimpleNamespace
    out = __import__("io").StringIO()
    args = SimpleNamespace(config=config, flame="ikuuu-primary", execute=False)
    assert login_window(args, out=out) == 0
    plan = json.loads(out.getvalue())
    assert plan["dry_run"] is True and plan["browser_opened"] is False and plan["network"] is False
    assert plan["checkin_performed"] is False and plan["cookies_read"] is False
    captured = capsys.readouterr()
    assert "uid=" not in captured.out + captured.err and "password" not in (captured.out + captured.err).casefold()


def test_login_execute_opens_the_window_and_does_not_check_in(tmp_path):
    profile = enroll(tmp_path)
    # Reuse enroll's marker; point config at it via the command helper.
    from keepflame.flames.ikuuu.cli import login_window
    from types import SimpleNamespace
    config = tmp_path / "config.toml"
    config.write_text(f"""
[storage]
path = "{tmp_path}/state.sqlite3"
[[flames]]
id = "ikuuu-primary"
keeper = "ikuuu"
account_id = "primary"
enabled = false
[flames.schedule]
timezone = "Asia/Shanghai"
first_window_start = "08:30"
first_window_end = "10:30"
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"
[flames.settings]
browser_profile_dir = "{profile}"
base_url = "https://ikuuu.pw"
cookie_file = "{tmp_path}/missing.cookie"
nav_seeds = ["https://ikuuu.bar/"]
""")
    html = challenge_login().body
    http = FakeHttp({("GET", "https://ikuuu.pw/auth/login"): response(200, "https://ikuuu.pw/auth/login", html)})
    seen = {}

    class Window:
        def open_login(self, url):
            seen["url"] = url

        def wait_closed(self, timeout_ms):
            seen["timeout"] = timeout_ms
            return "closed"

        def request(self, *_args, **_kwargs):
            raise AssertionError("login must not request check-in")

    @contextmanager
    def opener(**kwargs):
        seen["hosts"] = kwargs["allowed_hosts"]
        seen["headless"] = kwargs["headless"]
        yield Window()

    out = __import__("io").StringIO()
    args = SimpleNamespace(config=config, flame=None, execute=True)
    assert login_window(args, http=http, opener=opener, out=out) == 0
    record = json.loads(out.getvalue())
    assert record["checkin_performed"] is False and record["cookies_read"] is False
    assert record["window"] == "closed" and seen["url"] == "https://ikuuu.pw/auth/login"
    assert seen["headless"] is False and "static.geetest.com" in seen["hosts"]
    assert "SYNTHETIC" not in out.getvalue()


def test_everyday_chrome_directory_is_not_a_keepflame_profile(tmp_path):
    root = tmp_path / "Google" / "Chrome"
    root.mkdir(parents=True, mode=0o700)
    (tmp_path / "Google").chmod(0o700)
    profile = root / "Default"
    profile.mkdir(mode=0o700)
    marker = profile / ".keepflame-profile.json"
    marker.write_text(json.dumps({"version": 1, "account_id": "primary", "purpose": "keepflame"}, separators=(",", ":")))
    marker.chmod(0o600)
    assert everyday_chrome_profile(profile) is True
    with pytest.raises(ProfileNotAuthorized):
        validate_profile(str(profile), "primary")


def test_history_reports_stored_traffic_without_secret_metadata(tmp_path):
    import sqlite3
    from keepflame.flames.ikuuu.cli import history
    from keepflame.storage.sqlite import SQLiteStorage

    database = tmp_path / "state.sqlite3"
    SQLiteStorage(database)
    secret = "synthetic-password"
    connection = sqlite3.connect(database)
    connection.execute(
        """INSERT INTO keep_runs(
               run_id, flame_id, account_id, started_at, finished_at, status, message, metadata_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("run-1", "ikuuu-primary", "primary", "2026-11-06T00:59:00+00:00", "2026-11-06T00:59:01+00:00",
         "SUCCESS", "你获得了 144MB 流量",
         json.dumps({"traffic_gained": "144MB", "checkin_state": "checked_in", "password": secret, "cookie": "uid=1"})),
    )
    connection.commit()
    connection.close()
    config = tmp_path / "config.toml"
    config.write_text(f"""
[storage]
path = "{database}"
[[flames]]
id = "ikuuu-primary"
keeper = "ikuuu"
account_id = "primary"
enabled = false
[flames.schedule]
timezone = "Asia/Shanghai"
first_window_start = "08:30"
first_window_end = "10:30"
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"
[flames.settings]
cookie_file = "{tmp_path}/missing.cookie"
nav_seeds = ["https://ikuuu.bar/"]
""")
    out = __import__("io").StringIO()
    args = SimpleNamespace(config=config, flame="ikuuu-primary", since="2026-11-05", limit=20)
    assert history(args, out=out) == 0
    record = json.loads(out.getvalue())
    assert record["read_only"] is True and record["writes"] is False
    assert "started_at" in record["columns"] and "metadata_json" in record["columns"]
    observed = record["runs"][0]["observed"]
    assert observed["traffic_gained"] == "144MB" and observed["checkin_state"] == "checked_in"
    assert record["runs"][0]["secret_keys_omitted"] == 2
    assert secret not in out.getvalue() and "uid=1" not in out.getvalue()

    sparse = tmp_path / "sparse.sqlite3"
    connection = sqlite3.connect(sparse)
    connection.execute("CREATE TABLE keep_runs (flame_id TEXT, note TEXT)")
    connection.execute("INSERT INTO keep_runs VALUES ('ikuuu-primary', 'ignored')")
    connection.commit()
    connection.close()
    sparse_config = tmp_path / "sparse.toml"
    sparse_config.write_text(config.read_text().replace(str(database), str(sparse)))
    out = __import__("io").StringIO()
    args = SimpleNamespace(config=sparse_config, flame="ikuuu-primary", since="2026-11-05", limit=20)
    assert history(args, out=out) == 1
    sparse_record = json.loads(out.getvalue())
    assert sparse_record["error"] == "started_at_column_missing"
    assert sparse_record["columns"] == ["flame_id", "note"]
    assert "144MB" not in out.getvalue()
