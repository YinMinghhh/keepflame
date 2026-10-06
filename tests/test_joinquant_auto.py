"""Automatic action policy, with no browser launch or account access."""

from contextlib import contextmanager
from datetime import timedelta
import json

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.capabilities.browser import ProfileNotAuthorized, validate_profile
from keepflame.core.models import KeepResult, KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import FlowError, JoinQuantBrowserKeeper
from keepflame.flames.joinquant.matching import Match, MatchError
from tests.helpers import MemoryStorage, StaticSecrets
from tests.test_orchestrator import local
from tests.test_joinquant import run_keeper
from tests.helpers import FakeHttpClient, response


def unsigned():
    return run_keeper(FakeHttpClient(response(200, '{"code":"00000","data":{"taskList":{"login":{"status":0}}}}')))


class FakeBrowser:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    @contextmanager
    def open(self, **options):
        assert options["origin"] == "https://www.joinquant.com"
        self.opened += 1
        try:
            yield object()
        finally:
            self.closed += 1


class FakePage:
    def __init__(self, results, *, challenge_error=None, submit_error=None):
        self.results = iter(results)
        self.checks = 0
        self.challenges = 0
        self.answers = []
        self.challenge_error = challenge_error
        self.submit_error = submit_error

    def open(self):
        pass

    def inspect(self, now):
        self.checks += 1
        return next(self.results)

    def challenge(self):
        self.challenges += 1
        if self.challenge_error:
            raise self.challenge_error
        return {}, "synthetic"

    def submit(self, x):
        self.answers.append(x)
        if self.submit_error:
            raise self.submit_error


def execute(page, *, solver=lambda *_: Match(90, 20, 0.99, 0.4)):
    browser = FakeBrowser()
    secrets = StaticSecrets(AssertionError("the browser flow must not read cookie/password files"))
    context = KeeperContext(
        flame_id="auto", account_id="synthetic", settings={"mode":"browser", "browser_profile_dir":"/synthetic-profile"},
        capabilities=CapabilityRegistry({"browser": browser}), secrets=secrets,
        storage=MemoryStorage(), now=local(11, 9),
    )
    keeper = JoinQuantBrowserKeeper(page_factory=lambda *a, **k: page, solver=solver, sleeper=lambda _:None)
    result = keeper.keep(context)
    assert browser.opened == browser.closed == 1
    assert secrets.references == []
    return result


def test_auto_requires_backend_confirmation():
    page = FakePage([unsigned(), unsigned(), KeepResult(KeepStatus.SUCCESS, "verified")])
    result = execute(page)
    assert result.status is KeepStatus.SUCCESS and result.metadata["automatic_action"] is True
    assert page.challenges == 1 and page.answers == [90] and page.checks == 3


def test_success_before_action_does_not_claim_again():
    page = FakePage([KeepResult(KeepStatus.SUCCESS, "already done")])
    result = execute(page)
    assert result.status is KeepStatus.SUCCESS and result.metadata["automatic_action"] is False
    assert page.challenges == 0 and page.answers == []


def test_button_or_challenge_missing_does_not_fake_success():
    page = FakePage([unsigned(), unsigned()], challenge_error=FlowError("signin_button_changed"))
    result = execute(page)
    assert result.status is KeepStatus.FAILED
    assert result.metadata["reason"] == "signin_button_changed"


def test_success_without_challenge_still_requires_fresh_inspection():
    page = FakePage([unsigned(), KeepResult(KeepStatus.SUCCESS, "verified")], challenge_error=FlowError("challenge_not_received"))
    assert execute(page).status is KeepStatus.SUCCESS
    assert page.checks == 2 and page.answers == []


def test_validation_ack_is_not_completion():
    page = FakePage([unsigned(), unsigned(), unsigned(), unsigned()])
    result = execute(page)
    assert result.status is KeepStatus.WAITING_HUMAN and result.metadata["reason"] == "claim_unconfirmed"
    assert result.metadata["unconfirmed_reason"] == "completion_unconfirmed" and result.metadata["automation_paused"]
    assert page.challenges == 1 and page.answers == [90] and page.checks == 4


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_or_rate_limit_stops_before_mutation(status):
    state = run_keeper(FakeHttpClient(response(status, "login" if status == 403 else "denied")))
    page = FakePage([state])
    result = execute(page)
    assert result.status is not KeepStatus.SUCCESS
    assert page.challenges == 0 and page.answers == []


@pytest.mark.parametrize("reason", ["challenge_answer_rejected", "site_callback_changed", "validation_unconfirmed"])
def test_submission_failure_is_not_retried(reason):
    page = FakePage([unsigned()], submit_error=FlowError(reason))
    result = execute(page)
    assert result.status is KeepStatus.FAILED
    assert result.next_action_at == local(11, 15)
    assert page.answers == [90]


def test_ambiguous_matching_stops_before_submit():
    def reject(*_):
        raise MatchError("ambiguous_match")
    page = FakePage([unsigned()])
    result = execute(page, solver=reject)
    assert result.status is KeepStatus.FAILED and page.answers == []


def test_unexpected_errors_never_expose_data():
    def explode(*_):
        raise RuntimeError("sensitive synthetic browser error")
    result = execute(FakePage([unsigned()]), solver=explode)
    assert "sensitive" not in result.message and "sensitive" not in json.dumps(result.metadata)


def enroll_synthetic(path):
    path.mkdir(mode=0o700)
    marker = path / ".keepflame-profile.json"
    marker.write_text(json.dumps({"version":1,"account_id":"synthetic","purpose":"keepflame"}))
    marker.chmod(0o600)
    return marker


def test_profile_requires_private_explicit_account_enrollment(tmp_path):
    path = tmp_path / "profile"
    with pytest.raises(ProfileNotAuthorized):
        validate_profile(str(path), "synthetic")
    marker = enroll_synthetic(path)
    assert validate_profile(str(path), "synthetic") == path
    with pytest.raises(ProfileNotAuthorized):
        validate_profile(str(path), "another-account")
    path.chmod(0o755)
    with pytest.raises(ProfileNotAuthorized):
        validate_profile(str(path), "synthetic")
    path.chmod(0o700)
    marker.chmod(0o644)
    with pytest.raises(ProfileNotAuthorized):
        validate_profile(str(path), "synthetic")


def test_profile_rejects_symlink_and_repository_location(tmp_path):
    path = tmp_path / "profile"
    enroll_synthetic(path)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(ProfileNotAuthorized):
        validate_profile(str(alias), "synthetic")
    (tmp_path / ".git").mkdir()
    with pytest.raises(ProfileNotAuthorized):
        validate_profile(str(path), "synthetic")


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_challenge_http_rejection_stops_without_another_request(status):
    page = FakePage([unsigned()], challenge_error=FlowError("challenge_rejected", status, "86400"))
    result = execute(page)
    assert page.checks == 1 and page.challenges == 1 and page.answers == []
    assert result.status is (KeepStatus.WAITING_HUMAN if status in (401,403) else KeepStatus.FAILED)
    assert result.next_action_at == local(12,9)


def test_validation_rate_limit_preserves_long_server_backoff():
    page = FakePage([unsigned()], submit_error=FlowError("validation_http",429,"172800"))
    result = execute(page)
    assert result.status is KeepStatus.FAILED and result.next_action_at == local(13,9)
    assert page.checks == 1 and page.answers == [90]


def test_browser_session_expiry_requests_profile_refresh_not_cookie_file():
    state = run_keeper(FakeHttpClient(response(401,"denied")))
    result = execute(FakePage([state]))
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == "REVIEW_BROWSER_SESSION"
    assert result.metadata["execution_mode"] == "browser"
    assert "密钥文件" not in result.human_action.instructions


def test_owned_browser_restricts_destinations_and_closes_on_error(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from keepflame.capabilities.browser import IsolatedBrowser
    profile = tmp_path / "dedicated-profile"
    enroll_synthetic(profile)
    calls = {}

    class Session:
        def set_default_timeout(self, timeout):
            calls["timeout"] = timeout
        def route(self, pattern, handler):
            calls["route"] = handler
        def route_web_socket(self, pattern, handler):
            calls["websocket"] = handler
        def new_page(self):
            return object()
        def close(self):
            calls["closed"] = True

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=self)
        def __exit__(self, *args):
            pass
        def launch_persistent_context(self, directory, **options):
            calls["directory"] = directory
            calls["options"] = options
            return Session()

    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=Playwright))
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with IsolatedBrowser().open(profile_dir=str(profile), account_id="synthetic", origin="https://www.joinquant.com"):
            raise RuntimeError("synthetic failure")
    assert calls["closed"] and calls["directory"] == str(profile)
    assert calls["options"]["chromium_sandbox"] is True
    assert calls["options"]["service_workers"] == "block" and calls["options"]["accept_downloads"] is False

    for url, allowed in [("https://www.joinquant.com/path", True), ("https://cdn.joinquant.com/style.css", True),
                         ("https://elsewhere.invalid/", False), ("http://www.joinquant.com/", False),
                         ("https://www.joinquant.com:9443/", False)]:
        result = []
        route = SimpleNamespace(request=SimpleNamespace(url=url), fetch=lambda **_:SimpleNamespace(status=200, dispose=lambda:None), fulfill=lambda **_:result.append(True), abort=lambda *_:result.append(False))
        calls["route"](route)
        assert result == [allowed]
    closed = []
    calls["websocket"](SimpleNamespace(close=lambda:closed.append(True)))
    assert closed == [True]


@pytest.mark.parametrize("entry", ["Default", "Local State", "Default/Network/Cookies", "Default/Sessions/session-file"])
def test_profile_rejects_internal_data_links_without_reading_target(tmp_path, entry):
    profile=tmp_path/"profile"
    enroll_synthetic(profile)
    external=tmp_path/"unrelated-synthetic-data"
    external.mkdir()
    sentinel=external/"sentinel"
    sentinel.write_text("synthetic untouched value")
    link=profile/entry
    link.parent.mkdir(parents=True,exist_ok=True)
    link.symlink_to(external if entry=="Default" else sentinel)
    with pytest.raises(ProfileNotAuthorized):
        validate_profile(str(profile),"synthetic")
    assert sentinel.read_text()=="synthetic untouched value"


def test_profile_allows_chrome_singleton_lock_links(tmp_path):
    profile=tmp_path/"profile"
    enroll_synthetic(profile)
    (profile/"SingletonLock").symlink_to("synthetic-host-12345")
    (profile/"SingletonSocket").symlink_to("/tmp/synthetic-no-socket")
    (profile/"SingletonCookie").symlink_to("synthetic-cookie-marker")
    assert validate_profile(str(profile),"synthetic")==profile
