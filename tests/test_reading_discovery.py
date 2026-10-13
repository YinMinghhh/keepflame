"""Anonymous read-only article discovery; every probe is a mock."""

from datetime import datetime, timezone

import pytest

from keepflame.core.models import KeepStatus
from keepflame.flames.joinquant import discovery as disc
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper
from keepflame.flames.joinquant.discovery import Discovery, DiscoveryError, classify, discover
from keepflame.flames.joinquant.task import TaskConfigurationError, task_from_settings
from keepflame.storage.sqlite import SQLiteStorage
from tests.test_joinquant_reading import reading
from tests.test_reading_rotation import A, B, RotationStorage, run

HASH = "/view/community/detail/" + "a" * 32


def site(top, questions=()):
    calls = []
    def probe(article_id):
        calls.append(article_id)
        if article_id > top:
            return "missing"
        return "question" if article_id in questions else "article"
    return probe, calls


def test_classify_redirect_targets():
    assert classify(302, "https://www.joinquant.com" + HASH) == "article"
    assert classify(302, HASH + "?type=2") == "question"
    assert classify(302, "https://www.joinquant.com/default/index/404") == "missing"
    assert classify(302, "https://evil.invalid" + HASH) == "unknown"
    assert classify(200, "") == "unknown"
    assert classify(302, "/view/community/detail/12345") == "unknown"


def test_finds_newest_unread_plain_article_and_skips_questions_and_read():
    probe, calls = site(81562, questions={81561})
    sleeps = []
    found = discover({"81562", "81560"}, probe=probe, start=81500, sleep=sleeps.append)
    assert found.article_id == "81559" and found.reason == "found"
    assert 81546 <= found.top_id <= 81562 and found.requests == len(calls) <= 30
    assert len(sleeps) == len(calls) - 1 and all(s == 0.5 for s in sleeps)


def test_seed_above_top_searches_downward():
    probe, _ = site(81000)
    found = discover(set(), probe=probe, start=81500, sleep=lambda _: None)
    assert found.article_id == "81000"


def test_request_budget_and_network_failure_degrade_without_article():
    probe, calls = site(10**9)
    found = discover(set(), probe=probe, start=81500, max_requests=5, sleep=lambda _: None)
    assert found == Discovery(None, None, 5, "request_budget") and len(calls) == 5
    def broken(_):
        raise DiscoveryError("network")
    assert discover(set(), probe=broken, sleep=lambda _: None).reason == "network"


def test_anonymous_probe_sends_no_cookie_and_never_follows(monkeypatch):
    import urllib.error
    import urllib.request
    seen = []
    class Reply:
        status = 302
        headers = {"Location": HASH}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_open(self, request, timeout=None):
        seen.append((request.full_url, request.get_method(), dict(request.header_items()),
                     [type(h).__name__ for h in self.handlers]))
        return Reply()
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    assert disc.anonymous_probe(81500) == "article"
    url, method, headers, handlers = seen[0]
    assert url == "https://www.joinquant.com/post/81500" and method == "GET"
    assert "Cookie" not in headers and "HTTPCookieProcessor" not in handlers
    def failing(self, request, timeout=None):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", failing)
    with pytest.raises(DiscoveryError):
        disc.anonymous_probe(81500)


def test_settings_default_and_bounds():
    task = task_from_settings({"task": "reading", "article_urls": [A]})
    assert task.discovery is True and task.discovery_max_requests == 30
    only = task_from_settings({"task": "reading"})
    assert only.article_id is None and only.discovery is True
    for bad in ({"reading_discovery": "yes"}, {"reading_discovery_max_requests": 4},
                {"reading_discovery_max_requests": 61}, {"reading_discovery_max_requests": 10.0}):
        with pytest.raises(TaskConfigurationError):
            task_from_settings({"task": "reading", "article_urls": [A], **bad})


SETTINGS = {"task": "reading", "article_urls": [A, B], "browser_profile_dir": "/unused", "mode": "browser"}


def keeper_run(storage, results, discoverer):
    import tests.test_reading_rotation as rotation
    original = rotation.JoinQuantBrowserKeeper
    rotation.JoinQuantBrowserKeeper = lambda **kw: original(discoverer=discoverer, **kw)
    try:
        return run(SETTINGS, storage, results)
    finally:
        rotation.JoinQuantBrowserKeeper = original


def test_exhausted_list_reads_discovered_article_and_caches_top():
    storage = RotationStorage(read=[111, 222])
    seen = {}
    def fake(excluded, *, start, max_requests):
        seen.update(excluded=excluded, start=start, budget=max_requests)
        return Discovery("81559", 81560, 12, "found")
    result, page, _ = keeper_run(storage, [reading(), reading({"status": 0, "totalCount": 1}),
                                           reading({"status": 1})], fake)
    assert result.status is KeepStatus.SUCCESS and page.reads == 1
    assert [a for a, _ in storage.recorded] == ["81559"]
    assert {"111", "222"} <= seen["excluded"] and seen["budget"] == 30
    assert storage.tops[("joinquant", "synthetic")] == 81560


def test_discovery_failure_stops_with_alert_reason_and_no_reservation():
    storage = RotationStorage(read=[111, 222])
    result, page, _ = keeper_run(storage, [reading()],
                                 lambda *_, **__: Discovery(None, None, 3, "network"))
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.metadata["reason"] == "reading_discovery_unavailable"
    assert page.reads == 0 and storage.reservations == 0


def test_discovery_skipped_when_reward_is_already_claimable():
    storage = RotationStorage(read=[111, 222])
    def never(*_, **__):
        raise AssertionError("discovery must stay lazy")
    result, page, _ = keeper_run(storage, [reading({"status": 0, "totalCount": 1}), reading({"status": 1})], never)
    assert result.status is KeepStatus.SUCCESS and page.claims == 1


def test_sqlite_caches_discovery_top(tmp_path):
    storage = SQLiteStorage(tmp_path / "s.sqlite3")
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    assert storage.discovery_top("joinquant", "primary") is None
    storage.remember_discovery_top("joinquant", "primary", 81540, now)
    storage.remember_discovery_top("joinquant", "primary", 81560, now)
    assert storage.discovery_top("joinquant", "primary") == 81560


def test_real_opener_turns_a_refused_redirect_into_a_classified_302(monkeypatch):
    """Exercise the real handler chain; only the HTTPS transport is faked."""
    import io
    import email.message
    import urllib.request
    import urllib.response
    def https_open(self, request):
        headers = email.message.Message()
        headers["Location"] = HASH + "?type=2"
        reply = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
        reply.msg = "Found"
        return reply
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    assert disc.anonymous_probe(81500) == "question"


def test_config_without_any_article_uses_discovery_only():
    import tests.test_reading_rotation as rotation
    storage = RotationStorage()
    settings = {"task": "reading", "browser_profile_dir": "/unused", "mode": "browser"}
    calls = []
    def fake(excluded, *, start, max_requests):
        calls.append(excluded)
        return Discovery("81601", 81596, 17, "found")
    original = rotation.JoinQuantBrowserKeeper
    rotation.JoinQuantBrowserKeeper = lambda **kw: original(discoverer=fake, **kw)
    try:
        result, page, task = run(settings, storage, [reading(), reading({"status": 0, "totalCount": 1}),
                                                     reading({"status": 1})])
    finally:
        rotation.JoinQuantBrowserKeeper = original
    assert result.status is KeepStatus.SUCCESS and page.reads == 1 and len(calls) == 1
    assert [a for a, _ in storage.recorded] == ["81601"]


def test_generated_server_config_has_no_fixed_article():
    from pathlib import Path
    text = Path("deploy/keepflame.server.toml.example").read_text()
    assert "reading_discovery = true" in text
    assert "article_url" not in text
