"""Browser pool liveness, pre-request failures and pause release. Fakes only; no Chrome."""

from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.capabilities.browser import BrowserDisconnected, BrowserUnavailable
from keepflame.capabilities.browser_pool import BrowserPool
from keepflame.cli import _owner_browser_headless
from keepflame.core.config import AppConfig, FlameConfig, ScheduleConfig, StorageConfig
from keepflame.core.eligibility import execution_paused, pre_request_failure, pre_request_failure_allows_new_day
from keepflame.core.models import BusinessTarget, FlameState, KeepResult, KeepStatus, RunRecord
from keepflame.core.orchestrator import Orchestrator
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, sanitized_error
from keepflame.flames.joinquant.matching import Match
from keepflame.storage import SQLiteStorage
from tests.helpers import FakeNotifier, FixedClock, MemoryStorage, StaticRegistry, StaticSecrets
from tests.test_joinquant_auto import FakePage, unsigned
from tests.test_orchestrator import local
from tests.test_joinquant import run_keeper
from tests.helpers import FakeHttpClient, response


def signed():
    return run_keeper(FakeHttpClient(response(200, '{"code":"00000","data":{"taskList":{"login":{"status":1}}}}')))

ORIGIN = "https://www.joinquant.com"
OPTIONS = dict(profile_dir="/synthetic/profile", account_id="synthetic", origin=ORIGIN)
OCT_19 = datetime(2026, 10, 19, 2, 9, 58, tzinfo=timezone.utc)  # 10:09 Asia/Shanghai


class TargetClosedError(Exception):
    """Same class name Playwright raises for a quit browser."""


class Page:
    def __init__(self, context):
        self.context, self.closed = context, False

    def close(self):
        if self.context.dead and self.context.close_raises:
            raise TargetClosedError("Target page, context or browser has been closed")
        self.closed = True

    def is_closed(self):
        return self.closed

    def goto(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, *_args):
        pass


class Context:
    def __init__(self):
        self.all_pages, self.handlers, self.listeners = [], [], {}
        self.dead = False
        self.close_raises = True
        self.new_page_calls = 0

    @property
    def pages(self):
        return [page for page in self.all_pages if not page.closed]

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def quit(self, *, emit):
        """Model the user quitting Chrome. Playwright may not dispatch 'close' yet."""
        self.dead = True
        if emit:
            for handler in self.listeners.get("close", []):
                handler(self)

    def new_page(self):
        self.new_page_calls += 1
        if self.dead:
            raise TargetClosedError("Target page, context or browser has been closed")
        page = Page(self)
        self.all_pages.append(page)
        return page

    def route(self, _pattern, handler):
        self.handlers.append(handler)

    def unroute(self, _pattern, handler):
        if self.dead:
            raise TargetClosedError("Target page, context or browser has been closed")
        self.handlers.remove(handler)


class Browser:
    def __init__(self, *, restore_session=False):
        self.contexts, self.closed_contexts = [], []
        self.fail_launches = 0
        self.restore_session = restore_session
        self.headless_requests = []

    @contextmanager
    def session(self, **kwargs):
        self.headless_requests.append(kwargs["headless"])
        if self.fail_launches:
            self.fail_launches -= 1
            raise RuntimeError("synthetic chrome launch failure at /Users/someone/profile")
        context = Context()
        self.contexts.append(context)
        try:
            yield SimpleNamespace(page=context.new_page())
        finally:
            self.closed_contexts.append(context)
            if context.dead:
                raise TargetClosedError("Browser has been closed")


def use_once(pool, **options):
    with pool.open(**{**OPTIONS, **options}) as page:
        return page


# ---- 1) pool liveness -------------------------------------------------------

@pytest.mark.parametrize("emit", [True, False])
def test_quit_browser_is_relaunched_before_the_task_page(emit):
    browser = Browser()
    pool = BrowserPool(browser)
    pool.open_visible(**OPTIONS)
    first = browser.contexts[0]
    first.quit(emit=emit)
    page = use_once(pool, headless=False)
    assert page.context is browser.contexts[1]
    assert first in browser.closed_contexts  # the dead session was torn down
    assert pool.relaunch_count == 1 and pool.launch_count == 2 and not pool.closed
    assert browser.headless_requests == [False, False]  # relaunched in the same visible mode
    # A proactively detected quit never touches the dead context again.
    assert first.new_page_calls == (1 if emit else 2)
    assert pool.last_disconnect == ("disconnected_before_use" if emit else "disconnected_on_new_page")
    # The relaunched context is reused afterwards.
    use_once(pool, headless=False)
    assert len(browser.contexts) == 2


def test_relaunch_failure_raises_browser_disconnected_and_keeps_the_pool_usable():
    browser = Browser()
    pool = BrowserPool(browser)
    pool.open_visible(**OPTIONS)
    browser.contexts[0].quit(emit=True)
    browser.fail_launches = 1
    with pytest.raises(BrowserDisconnected) as raised:
        use_once(pool)
    assert "/Users" not in str(raised.value)
    assert not pool.closed and pool._contexts == {}
    # Chrome can start again later; the next run relaunches instead of staying stuck.
    page = use_once(pool)
    assert page.context is browser.contexts[-1] and not pool.closed


def test_relaunched_browser_that_dies_again_is_reported_not_retried_forever():
    class Flaky(Browser):
        @contextmanager
        def session(self, **kwargs):
            with super().session(**kwargs) as owned:
                owned.page.context.dead = True
                owned.page.context.close_raises = False
                yield owned

    browser = Flaky()
    pool = BrowserPool(browser)
    with pytest.raises(BrowserDisconnected):
        use_once(pool)
    assert len(browser.contexts) == 2 and not pool.closed


def test_ordinary_new_page_error_on_a_live_browser_is_not_a_relaunch():
    browser = Browser()
    pool = BrowserPool(browser)
    pool.open_visible(**OPTIONS)

    def broken():
        raise ValueError("synthetic page error")

    browser.contexts[0].new_page = broken
    with pytest.raises(ValueError):
        use_once(pool)
    assert pool.relaunch_count == 0 and len(browser.contexts) == 1


def test_restored_owner_is_never_relaunched_silently():
    browser = Browser(restore_session=True)
    pool = BrowserPool(browser)
    pool.open_visible(**OPTIONS)
    browser.contexts[0].quit(emit=True)
    with pytest.raises(BrowserDisconnected):
        use_once(pool)
    assert pool.closed and pool.close_reason == "browser_disconnected" and len(browser.contexts) == 1


def test_browser_quit_during_a_task_discards_the_slot_instead_of_stopping_the_pool():
    browser = Browser()
    pool = BrowserPool(browser)
    with pytest.raises(BrowserDisconnected):
        with pool.open(**OPTIONS) as page:
            page.context.quit(emit=True)
    assert not pool.closed and pool._contexts == {}
    assert use_once(pool).context is browser.contexts[1]


def test_live_cleanup_failure_still_stops_the_pool():
    browser = Browser()
    pool = BrowserPool(browser)
    with pytest.raises(BrowserUnavailable) as raised:
        with pool.open(**OPTIONS) as page:
            def fail():
                raise RuntimeError("synthetic close failure")
            page.close = fail
    assert not isinstance(raised.value, BrowserDisconnected)
    assert pool.closed and pool.close_reason == "security_cleanup_failed"


def test_login_window_open_after_quit_relaunches_and_dead_login_cleanup_does_not_stop_the_pool():
    browser = Browser()
    pool = BrowserPool(browser)
    pool.open_visible(**OPTIONS)
    browser.contexts[0].quit(emit=True)
    pool.begin_login(**OPTIONS, login_url=ORIGIN + "/user/login", blocked_paths=("/credits/",))
    login_context = browser.contexts[1]
    assert pool.login_window_open(OPTIONS["profile_dir"])
    login_context.quit(emit=True)
    with pytest.raises(BrowserDisconnected):
        pool.end_login(OPTIONS["profile_dir"])
    assert not pool.closed
    assert use_once(pool).context is browser.contexts[2]


# ---- 1/2/3) keeper result -------------------------------------------------------

class RaisingBrowser:
    def __init__(self, exc):
        self.exc = exc

    @contextmanager
    def open(self, **_options):
        raise self.exc
        yield  # pragma: no cover


def keep(browser, page=None, *, solver=lambda *_: Match(90, 20, 0.99, 0.4)):
    context = KeeperContext(
        flame_id="joinquant-primary", account_id="synthetic",
        settings={"mode": "browser", "browser_profile_dir": "/synthetic-profile", "browser_headless": True},
        capabilities=CapabilityRegistry({"browser": browser}), secrets=StaticSecrets("unused"),
        storage=MemoryStorage(), now=local(11, 9),
    )
    keeper = JoinQuantBrowserKeeper(page_factory=lambda *a, **k: page, solver=solver, sleeper=lambda _: None)
    return keeper.keep(context)


def test_browser_unavailable_before_the_page_is_a_bounded_failure_not_a_pause():
    result = keep(RaisingBrowser(BrowserDisconnected("browser relaunch failed: RuntimeError")))
    assert result.status is KeepStatus.FAILED and result.human_action is None
    metadata = result.metadata
    assert metadata["reason"] == "browser_unavailable"
    assert "automation_paused" not in metadata and "pause_account" not in metadata
    assert metadata["pre_request_failure"] is True and "stage_counts" not in metadata
    assert metadata["browser_disconnected"] is True
    assert metadata["error_type"] == "BrowserDisconnected"
    assert metadata["error_detail"] == "browser_relaunch_failed"
    assert result.next_action_at is None  # the core retry ladder schedules it


def test_unexpected_pre_request_error_logs_type_and_category_without_urls_or_cookies():
    exc = TargetClosedError("Target page, context or browser has been closed\n"
                            "call log: GET https://www.joinquant.com/x?token=abc Cookie: uid=SYNTHETIC")
    result = keep(RaisingBrowser(exc))
    text = json.dumps(result.metadata)
    assert result.status is KeepStatus.FAILED
    assert result.metadata["reason"] == "browser_operation_failed"
    assert result.metadata["error_type"] == "TargetClosedError"
    assert result.metadata["error_detail"] == "target_closed"
    assert "automation_paused" not in result.metadata and result.metadata["pre_request_failure"] is True
    for forbidden in ("https://", "joinquant.com/x", "token", "SYNTHETIC", "Cookie"):
        assert forbidden not in text


def test_error_after_the_task_page_keeps_its_stop_and_reports_only_a_category():
    def explode(*_):
        raise RuntimeError("sensitive synthetic text https://example.invalid/?a=b")
    pool = BrowserPool(Browser())
    result = keep(pool, FakePage([unsigned()]), solver=explode)
    pool.close()
    assert result.metadata["error_type"] == "RuntimeError"
    assert result.metadata["error_detail"] == "unclassified"
    assert "pre_request_failure" not in result.metadata
    assert "sensitive" not in json.dumps(result.metadata)


@pytest.mark.parametrize("message,label", [
    ("Timeout 5000ms exceeded.", "timeout"),
    ("page.goto: net::ERR_CONNECTION_REFUSED at https://x.invalid/?q=1", "net::ERR_CONNECTION_REFUSED"),
    ("Browser has disconnected!", "browser_disconnected"),
    ("uid=1; key=SECRET", "unclassified"),
])
def test_error_categories_are_fixed_labels(message, label):
    assert sanitized_error(RuntimeError(message)) == label


def test_incident_quit_browser_now_relaunches_and_checks_in():
    browser = Browser()
    pool = BrowserPool(browser)
    pool.open_visible(**{**OPTIONS, "profile_dir": "/synthetic-profile"})
    browser.contexts[0].quit(emit=False)
    page = FakePage([signed()])
    result = keep(pool, page)
    pool.close()
    assert pool.relaunch_count == 1
    assert result.status is KeepStatus.SUCCESS, result.metadata
    assert page.checks == 1  # the task reached the status check on the relaunched browser


# ---- 3) orchestrator + SQLite pause behaviour -------------------------------------

def flame():
    return FlameConfig(
        id="joinquant-primary", keeper="joinquant", account_id="primary", enabled=True,
        schedule=ScheduleConfig(timezone="Asia/Shanghai", first_check=time(9), recheck_minutes=60,
                                reminder_at=time(20), stop_at=time(23)),
        settings={"mode": "browser", "task": "checkin", "browser_profile_dir": "/synthetic-profile",
                  "browser_headless": True},
    )


def test_incident_relaunch_failure_retries_on_the_ladder_and_never_pauses(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    browser = Browser()
    pool = BrowserPool(browser)
    pool.open_visible(**{**OPTIONS, "profile_dir": "/synthetic-profile", "account_id": "primary"})
    browser.contexts[0].quit(emit=False)
    browser.fail_launches = 1
    orchestrator = Orchestrator(
        storage=store, registry=StaticRegistry(JoinQuantBrowserKeeper(sleeper=lambda _: None)),
        capabilities=CapabilityRegistry({"browser": pool}), secrets=StaticSecrets("unused"),
        notifier=FakeNotifier(), clock=FixedClock(OCT_19),
    )
    outcome = orchestrator.run_flame(flame(), now=OCT_19)
    pool.close()
    metadata = outcome.record.result.metadata
    assert outcome.record.result.status is KeepStatus.FAILED
    assert metadata["reason"] == "browser_unavailable" and metadata["pre_request_failure"] is True
    assert store.is_paused("joinquant", "primary", "joinquant-primary") is False
    assert execution_paused(store, "joinquant", "primary", "joinquant-primary", now=OCT_19) is False
    assert outcome.state.next_action_at == OCT_19 + timedelta(minutes=30)
    assert metadata["retry_attempt"] == 1


def seed(store, metadata, *, flame_id="joinquant-primary"):
    started = OCT_19
    result = KeepResult(KeepStatus.FAILED, "JoinQuant automatic task stopped", metadata=metadata)
    target = BusinessTarget("2026-10-19", "Asia/Shanghai")
    store.record(RunRecord(run_id=str(uuid4()), flame_id=flame_id, account_id="primary",
                           started_at=started, finished_at=started, result=result, business_target=target),
                 FlameState(flame_id=flame_id, account_id="primary", status=KeepStatus.FAILED,
                            message=result.message, last_checked_at=started, last_success_at=None,
                            consecutive_failures=1, pending_human_since=None, human_action=None,
                            next_action_at=None, last_reminder_at=None, metadata=metadata,
                            last_checked_target=target))
    store.pause("joinquant", "primary", flame_id, str(metadata.get("reason")), started)


NEXT_DAY = OCT_19 + timedelta(days=1)


def test_pre_request_pause_expires_on_the_next_business_day(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    seed(store, {"reason": "browser_operation_failed", "pre_request_failure": True, "automation_paused": True})
    assert store.blocks_execution("joinquant", "primary", "joinquant-primary", now=OCT_19) is True
    assert store.blocks_execution("joinquant", "primary", "joinquant-primary", now=NEXT_DAY) is False
    assert store.is_paused("joinquant", "primary", "joinquant-primary") is False


@pytest.mark.parametrize("metadata", [
    # The 10/19 record shape: no claim count and no pre-request marker stays strict.
    {"reason": "browser_operation_failed", "automation_paused": True, "action_stage": "inspect"},
    # Any recorded stage count means a request path existed; the claim rule decides.
    {"reason": "browser_operation_failed", "pre_request_failure": True, "stage_counts": {"claim": 1}},
    # Core safety stops are never auto-released.
    {"reason": "clock_regressed", "pre_request_failure": True},
    {"reason": "browser_operation_failed", "pre_request_failure": "yes"},
])
def test_other_pauses_without_proof_stay_until_resume(tmp_path, metadata):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    seed(store, metadata)
    assert store.blocks_execution("joinquant", "primary", "joinquant-primary", now=NEXT_DAY) is True


def test_account_wide_pause_is_never_released_by_a_pre_request_marker(tmp_path):
    store = SQLiteStorage(tmp_path / "state.sqlite3")
    seed(store, {"reason": "browser_operation_failed", "pre_request_failure": True})
    store.pause("joinquant", "primary", "*", "login_required", OCT_19)
    assert store.blocks_execution("joinquant", "primary", "joinquant-primary", now=NEXT_DAY) is True


def test_pre_request_eligibility_rules():
    marker = {"pre_request_failure": True, "reason": "browser_unavailable"}
    assert pre_request_failure(marker)
    assert pre_request_failure_allows_new_day("2026-10-18", marker, "2026-10-19")
    assert not pre_request_failure_allows_new_day("2026-10-19", marker, "2026-10-19")
    assert not pre_request_failure({**marker, "stage_counts": {}})
    assert not pre_request_failure(None)


# ---- 4) owner log browser_headless ------------------------------------------------

def test_http_only_flame_does_not_flip_the_owner_browser_headless_report(tmp_path):
    schedule = ScheduleConfig(timezone="Asia/Shanghai", first_check=time(9), recheck_minutes=60,
                              reminder_at=time(20), stop_at=time(23))
    profile = "/synthetic-profile"
    browser_flames = tuple(
        FlameConfig(name, "joinquant", "primary", True, schedule,
                    {"mode": "browser", "task": task, "browser_profile_dir": profile, "browser_headless": True})
        for name, task in (("joinquant-primary", "checkin"), ("joinquant-reading-primary", "reading")))
    ikuuu = FlameConfig("ikuuu-primary", "ikuuu", "primary", True, schedule, {"cookie_file": "/x/ikuuu.cookie"})
    targets = [(profile, "primary", ORIGIN)]
    config = AppConfig(StorageConfig(tmp_path / "s.sqlite3"), browser_flames + (ikuuu,), tmp_path / "c.toml")
    assert _owner_browser_headless(config, targets) is True
    mixed = AppConfig(config.storage, (browser_flames[0], FlameConfig(
        "joinquant-reading-primary", "joinquant", "primary", True, schedule,
        {"mode": "browser", "task": "reading", "browser_profile_dir": profile, "browser_headless": False}), ikuuu),
        config.source_path)
    assert _owner_browser_headless(mixed, targets) is False
    assert _owner_browser_headless(AppConfig(config.storage, (ikuuu,), config.source_path), []) is False
