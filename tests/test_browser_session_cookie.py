"""A no-expiry httpOnly cookie stays inside one synthetic profile across processes."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from keepflame.capabilities.http import HttpResponse
from keepflame.capabilities.session_cookie import STORE_NAME, records_from_set_cookie
from keepflame.core.models import KeepStatus
from keepflame.flames.joinquant.keeper import JoinQuantKeeper

FIXTURE_NAME = ".synthetic-session-secret"
COOKIE_NAME = "synthetic_session"
ORIGIN = "https://www.joinquant.com"


def _enroll(path: Path, account_id: str) -> None:
    path.mkdir(mode=0o700)
    marker = path / ".keepflame-profile.json"
    marker.write_text(json.dumps({"version": 1, "account_id": account_id, "purpose": "keepflame"}))
    marker.chmod(0o600)


def _run_child(phase: str, profile: Path, account_id: str, report: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "KEEPFLAME_COOKIE_CHILD": "1",
        "KEEPFLAME_COOKIE_PHASE": phase,
        "KEEPFLAME_COOKIE_PROFILE": str(profile),
        "KEEPFLAME_COOKIE_ACCOUNT": account_id,
        "KEEPFLAME_COOKIE_ORIGIN": ORIGIN,
        "KEEPFLAME_COOKIE_REPORT": str(report),
    })
    return subprocess.run(
        [sys.executable, "-m", "tests.test_browser_session_cookie"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _files_containing(root: Path, secret: str, *, skip: Path | None = None) -> list[Path]:
    found = []
    needle = secret.encode()
    for path in root.rglob("*"):
        if skip is not None and (path == skip or skip in path.parents):
            continue
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes():
            found.append(path)
    return found


def test_session_cookie_survives_process_exit_inside_one_profile(tmp_path):
    profile = tmp_path / "synthetic-profile"
    other = tmp_path / "other-account-profile"
    _enroll(profile, "synthetic")
    _enroll(other, "other-account")
    secret = os.urandom(16).hex()
    fixture = profile / FIXTURE_NAME
    fixture.write_text(secret)
    fixture.chmod(0o600)

    retained = _run_child("retain", profile, "synthetic", tmp_path / "retain-report.json")
    assert retained.returncode == 0, retained.stderr.replace(secret, "<redacted>")
    assert secret not in retained.stdout and secret not in retained.stderr

    store = profile / STORE_NAME
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    saved = json.loads(store.read_text())
    assert saved["account_id"] == "synthetic" and saved["origin"] == ORIGIN
    assert [cookie["name"] for cookie in saved["cookies"]] == [COOKIE_NAME]
    assert saved["cookies"][0]["value"] == secret
    assert saved["cookies"][0]["httpOnly"] is True and saved["cookies"][0]["expires"] > 0
    assert "persistent-fixture" not in store.read_text() and "visible-fixture" not in store.read_text()
    inside = {path.name for path in _files_containing(profile, secret)}
    assert inside == {FIXTURE_NAME, STORE_NAME}

    report_path = tmp_path / "send-report.json"
    sent = _run_child("send", profile, "synthetic", report_path)
    assert sent.returncode == 0, sent.stderr.replace(secret, "<redacted>")
    assert secret not in sent.stdout and secret not in sent.stderr
    report = json.loads(report_path.read_text())
    expected = hashlib.sha256(f"{COOKIE_NAME}={secret}".encode()).hexdigest()
    assert report["cookie_count"] == 1, "second process did not send the retained session cookie"
    assert report["cookie_sha256"] == expected, "second process did not send the retained session cookie"
    assert report["names"] == [COOKIE_NAME]
    assert secret not in report_path.read_text()
    assert _files_containing(tmp_path, secret, skip=profile) == []
    assert _files_containing(other, secret) == []

    refused = _run_child("send", other, "other-account", tmp_path / "other-report.json")
    assert refused.returncode == 0, refused.stderr.replace(secret, "<redacted>")
    other_report = json.loads((tmp_path / "other-report.json").read_text())
    assert other_report["cookie_count"] == 0
    assert _files_containing(other, secret) == []
    assert secret not in refused.stdout and secret not in refused.stderr

    result = JoinQuantKeeper._interpret(
        HttpResponse(200, ORIGIN + "/credits/task/getTaskInfo", {}, b'{"code":"10001","msg":"login"}'),
        now=datetime.now(timezone.utc),
    )
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.status is not KeepStatus.SUCCESS
    print("observation session_cookie_written_before_handler_return=true")
    print("observation session_cookie_survives_closed_handle=true")
    print("observation session_cookie_second_process=sent")
    print("observation cookie_value_outside_profile=absent")
    print("observation other_account_profile=absent")
    print("observation remote_code_10001=WAITING_HUMAN")


def test_records_ignore_persistent_and_script_readable_cookies():
    now = 1_700_000_000.0
    url = ORIGIN + "/user/login/index"
    session = f"{COOKIE_NAME}=local-fixture; Path=/; Secure; HttpOnly; SameSite=Lax"
    persistent = "synthetic_persistent=persistent-fixture; Path=/; Secure; HttpOnly; Max-Age=3600"
    readable = "synthetic_visible=visible-fixture; Path=/; Secure; SameSite=Lax"
    records = records_from_set_cookie([session, persistent, readable], request_url=url, now=now)
    assert [record["name"] for record in records] == [COOKIE_NAME]
    assert records[0]["expires"] == now + 30 * 24 * 60 * 60
    assert "value" in records[0] and records[0]["httpOnly"] is True


def _close_store_handles(store: Path) -> None:
    """Drop any process-side descriptor. The file has to remain after that."""
    names = {str(store), str(store.parent / f".{STORE_NAME}.tmp")}
    for fd in range(3, 256):
        try:
            linked = os.readlink(f"/dev/fd/{fd}")
        except OSError:
            continue
        if linked in names or linked.startswith(str(store)):
            os.close(fd)


def _child() -> int:
    phase = os.environ["KEEPFLAME_COOKIE_PHASE"]
    profile = Path(os.environ["KEEPFLAME_COOKIE_PROFILE"])
    account_id = os.environ["KEEPFLAME_COOKIE_ACCOUNT"]
    origin = os.environ["KEEPFLAME_COOKIE_ORIGIN"]
    report_path = Path(os.environ["KEEPFLAME_COOKIE_REPORT"])
    secret = ""
    if phase == "retain":
        secret = (profile / FIXTURE_NAME).read_text().strip()

    class Response:
        status = 200

        def headers_array(self):
            if phase != "retain":
                return []
            return [
                {"name": "Set-Cookie", "value": f"{COOKIE_NAME}={secret}; Path=/; Secure; HttpOnly; SameSite=Lax"},
                {"name": "Set-Cookie", "value": "synthetic_persistent=persistent-fixture; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=3600"},
                {"name": "Set-Cookie", "value": "synthetic_visible=visible-fixture; Path=/; Secure; SameSite=Lax"},
            ]

        def dispose(self):
            return None

    class Page:
        def __init__(self, context):
            self.context = context

        def goto(self, url):
            parts = []
            host = urlsplit(url).hostname
            for cookie in self.context.jar:
                target = cookie.get("url")
                domain = cookie.get("domain")
                if isinstance(target, str) and urlsplit(target).hostname == host:
                    parts.append(f"{cookie['name']}={cookie['value']}")
                elif isinstance(domain, str) and (host == domain or (host or "").endswith("." + domain)):
                    parts.append(f"{cookie['name']}={cookie['value']}")
            header = "; ".join(parts)
            report_path.write_text(json.dumps({
                "phase": phase,
                "cookie_count": len(parts),
                "cookie_sha256": hashlib.sha256(header.encode()).hexdigest() if header else "",
                "names": [cookie.get("name") for cookie in self.context.jar],
            }))

            def fetch(**_options):
                return Response()

            def fulfill(**_options):
                if phase != "retain":
                    return None
                store = profile / STORE_NAME
                if not store.is_file() or stat.S_IMODE(store.stat().st_mode) != 0o600:
                    os._exit(2)
                _close_store_handles(store)
                try:
                    saved = json.loads(store.read_text())
                except (OSError, ValueError):
                    os._exit(3)
                if saved.get("account_id") != account_id or not saved.get("cookies"):
                    os._exit(4)
                if not store.is_file() or store.stat().st_size <= 0:
                    os._exit(5)
                os._exit(0)

            route = SimpleNamespace(
                request=SimpleNamespace(url=url),
                fetch=fetch,
                fulfill=fulfill,
                abort=lambda *_args, **_kwargs: None,
            )
            self.context.handler(route)

    class Context:
        def __init__(self):
            self.jar: list[dict] = []
            self.handler = None

        def set_default_timeout(self, _timeout):
            return None

        def add_cookies(self, cookies):
            self.jar.extend(cookies)

        def cookies(self, *_args, **_kwargs):
            raise RuntimeError("cookie export is forbidden")

        def storage_state(self, *_args, **_kwargs):
            raise RuntimeError("storage export is forbidden")

        def route(self, _pattern, handler):
            self.handler = handler

        def route_web_socket(self, _pattern, _handler):
            return None

        def new_page(self):
            return Page(self)

        def close(self):
            return None

    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=lambda *_args, **_kwargs: Context(),
        ))

    sys.modules["playwright.sync_api"] = SimpleNamespace(sync_playwright=playwright)
    from keepflame.capabilities.browser import IsolatedBrowser

    with IsolatedBrowser().session(profile_dir=str(profile), account_id=account_id, origin=origin, headless=False) as session:
        session.page.goto(origin + "/user/login/index")
    return 0


if __name__ == "__main__" and os.environ.get("KEEPFLAME_COOKIE_CHILD") == "1":
    raise SystemExit(_child())
