"""Daily article rotation, read history and the slow scroll; no external I/O."""

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import time

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.models import KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant import browser as browser_module
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN
from keepflame.flames.joinquant.matching import Match
from keepflame.flames.joinquant.task import (DEFAULT_READING_WAIT_SECONDS, TaskConfigurationError,
                                             choose_article, task_from_settings)
from keepflame.storage.sqlite import SQLiteStorage
from tests.helpers import StaticSecrets
from tests.test_joinquant_reading import Gateway, JournalStorage, reading
from tests.test_orchestrator import local

A, B, C = (JOINQUANT_ORIGIN + f"/view/community/detail/{n}" for n in (111, 222, 333))


def test_legacy_single_article_still_parses_with_new_default_wait():
    task = task_from_settings({"task": "reading", "article_url": A})
    assert task.article_id == "111" and task.article_candidates == ((A, "111"),)
    assert task.wait_seconds == DEFAULT_READING_WAIT_SECONDS and 45 <= task.wait_seconds <= 60
    assert task.scroll is True


def test_list_follows_legacy_article_and_rejects_bad_entries():
    task = task_from_settings({"task": "reading", "article_url": A, "article_urls": [B, C]})
    assert [i for _, i in task.article_candidates] == ["111", "222", "333"]
    for bad in ([], [A, A], [B, B], [A + "?x=1"], "not-a-list", [None]):
        with pytest.raises(TaskConfigurationError):
            task_from_settings({"task": "reading", "article_urls": bad})
    with pytest.raises(TaskConfigurationError):
        task_from_settings({"task": "reading", "article_url": A, "article_urls": [A]})
    with pytest.raises(TaskConfigurationError):
        task_from_settings({"task": "reading", "article_urls": [A], "reading_scroll": "yes"})
    with pytest.raises(TaskConfigurationError):
        task_from_settings({"task": "reading", "reading_discovery": False})


def test_choose_skips_read_articles_and_exhausts_without_repeat():
    task = task_from_settings({"task": "reading", "article_urls": [A, B]})
    assert choose_article(task, {"111"}).article_id == "222"
    exhausted = choose_article(task, {"111", "222"})
    assert exhausted.article_id is None and exhausted.article_url is None


class RotationStorage(JournalStorage):
    def __init__(self, read=()):
        super().__init__()
        self.read = {str(i): "2026-10-01" for i in read}
        self.recorded = []
    def read_article_ids(self, keeper, account_id):
        return set(self.read)
    def record_article_read(self, keeper, account_id, article_id, day, now):
        self.recorded.append((article_id, day))
        self.read.setdefault(article_id, day)


def run(settings, storage, results):
    gateway = Gateway(results, storage)
    seen = {}
    def factory(page, **options):
        seen["task"] = options.get("task")
        return gateway
    @contextmanager
    def browser(**kwargs):
        yield object()
    context = KeeperContext("reading", "synthetic", settings, CapabilityRegistry({"browser": SimpleNamespace(open=browser)}),
                            StaticSecrets(AssertionError("no credentials")), storage, local(11, 9))
    result = JoinQuantBrowserKeeper(page_factory=factory, solver=lambda *_: Match(90, 0, .99, .4),
                                    sleeper=lambda _: None).keep(context)
    return result, gateway, seen.get("task")


SETTINGS = {"task": "reading", "article_urls": [A, B, C], "browser_profile_dir": "/unused-synthetic", "mode": "browser",
            "reading_discovery": False}


def test_daily_run_reads_first_unread_article_and_records_it():
    storage = RotationStorage(read=[111])
    result, page, task = run(SETTINGS, storage,
                             [reading(), reading({"status": 0, "totalCount": 1}), reading({"status": 1})])
    assert result.status is KeepStatus.SUCCESS
    assert task.article_id == "222" and page.reads == 1
    assert [a for a, _ in storage.recorded] == ["222"]


def test_unconfirmed_read_is_still_recorded_so_tomorrow_rotates():
    storage = RotationStorage()
    result, page, task = run(SETTINGS, storage, [reading(), reading()])
    assert result.metadata["reason"] == "claim_unconfirmed"
    assert [a for a, _ in storage.recorded] == ["111"]


def test_exhausted_list_stops_with_clear_reason_and_no_reservation():
    storage = RotationStorage(read=[111, 222, 333])
    result, page, _ = run(SETTINGS, storage, [reading()])
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.metadata["reason"] == "reading_articles_exhausted"
    assert page.reads == 0 and storage.reservations == 0 and storage.recorded == []


def test_exhausted_list_does_not_block_an_already_claimable_reward():
    storage = RotationStorage(read=[111, 222, 333])
    result, page, _ = run(SETTINGS, storage, [reading({"status": 0, "totalCount": 1}), reading({"status": 1})])
    assert result.status is KeepStatus.SUCCESS and page.reads == 0 and page.claims == 1


def test_sqlite_records_article_reads_once_per_account(tmp_path):
    storage = SQLiteStorage(tmp_path / "state.sqlite3")
    now = datetime(2026, 10, 13, 8, tzinfo=timezone.utc)
    assert storage.read_article_ids("joinquant", "primary") == set()
    storage.record_article_read("joinquant", "primary", "111", "2026-10-13", now)
    storage.record_article_read("joinquant", "primary", "111", "2026-10-14", now)
    storage.record_article_read("joinquant", "other", "222", "2026-10-13", now)
    assert storage.read_article_ids("joinquant", "primary") == {"111"}
    with pytest.raises(ValueError):
        storage.record_article_read("joinquant", "primary", "333", "13/10/2026", now)
    # Reopening an existing database keeps history and the schema version.
    again = SQLiteStorage(tmp_path / "state.sqlite3")
    assert again.read_article_ids("joinquant", "primary") == {"111"}
    again.healthcheck()


class ScrollPage:
    def __init__(self, url):
        self.url = url
        self.mouse = SimpleNamespace(wheel=self.wheel)
        self.wheels = []
        self.context = SimpleNamespace(pages=[])
    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))
    def goto(self, url, **kwargs):
        return SimpleNamespace(status=200)
    def is_closed(self):
        return False
    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000 / 50)


class ReadyGuard:
    article_allowed = True
    counts = {"/community/post/postView": 1}
    def check(self):
        pass
    def begin_reading(self, **kwargs):
        pass


@pytest.mark.parametrize("scroll", [True, False])
def test_read_article_scrolls_slowly_downward_only_when_enabled(monkeypatch, scroll):
    monkeypatch.setattr(browser_module, "SCROLL_START_SECONDS", 0.0)
    monkeypatch.setattr(browser_module, "SCROLL_INTERVAL_SECONDS", 0.01)
    task = task_from_settings({"task": "reading", "article_urls": [A], "reading_wait_seconds": 30,
                               "reading_scroll": scroll})
    page = ScrollPage(A)
    gateway = JoinQuantPage.__new__(JoinQuantPage)
    gateway.page, gateway.task, gateway.guard = page, task, ReadyGuard()
    gateway.deadline = time.monotonic() + 600
    clock = iter(range(0, 10_000))
    start = time.monotonic()
    monkeypatch.setattr(browser_module.time, "monotonic", lambda: start + next(clock) * 0.2)
    gateway.read_article()
    if scroll:
        assert 0 < len(page.wheels) <= browser_module.SCROLL_STEPS
        assert all(dx == 0 and dy > 0 for dx, dy in page.wheels)
    else:
        assert page.wheels == []


class UnsupportedGateway(Gateway):
    def __init__(self, results, storage, reason):
        super().__init__(results, storage)
        self.guard = SimpleNamespace(counts=Counter(),
                                     failure=SimpleNamespace(reason=reason, blocked_request=None))
    def read_article(self):
        from keepflame.flames.joinquant.guard import FlowError
        raise FlowError(self.guard.failure.reason)


def _run_with(gateway, storage):
    @contextmanager
    def browser(**kwargs):
        yield object()
    context = KeeperContext("reading", "synthetic", SETTINGS, CapabilityRegistry({"browser": SimpleNamespace(open=browser)}),
                            StaticSecrets(AssertionError("no credentials")), storage, local(11, 9))
    return JoinQuantBrowserKeeper(page_factory=lambda *a, **k: gateway, solver=lambda *_: Match(90, 0, .99, .4),
                                  sleeper=lambda _: None).keep(context)


def test_unsupported_article_is_remembered_so_next_run_moves_on():
    storage = RotationStorage()
    result = _run_with(UnsupportedGateway([reading()], storage, "article_not_supported"), storage)
    assert result.status is not KeepStatus.SUCCESS
    assert [a for a, _ in storage.recorded] == ["111"]
    assert choose_article(task_from_settings(SETTINGS), storage.read).article_id == "222"


def test_other_failures_before_registration_are_not_remembered():
    storage = RotationStorage()
    _run_with(UnsupportedGateway([reading()], storage, "article_navigation_changed"), storage)
    assert storage.recorded == []
