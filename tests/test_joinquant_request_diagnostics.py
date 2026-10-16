"""Article startup compatibility and first-failure privacy, with no external I/O."""

from contextlib import contextmanager
from io import StringIO
import json
from types import SimpleNamespace

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.cli import _outcome_dict
from keepflame.core.models import KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper
from keepflame.flames.joinquant.guard import ALGORITHM_LIST_PATH, FlowError, READING_PATH
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN
from keepflame.notifier.jsonl import JsonlNotifier
from tests.helpers import MemoryStorage, StaticSecrets
from tests.test_joinquant_guard import Reply, Route
from tests.test_joinquant_reading import SETTINGS, reading_guard
from tests.test_orchestrator import local


def algorithm_list(guard, page, *, body=None, resource_type="xhr"):
    request = Route(Reply(), url=JOINQUANT_ORIGIN+ALGORITHM_LIST_PATH)
    request.request.post_data = body
    request.request.resource_type = resource_type
    request.request.frame = page.main_frame
    return request


@pytest.mark.parametrize("body", [None, ""])
@pytest.mark.parametrize("resource_type", ["xhr", "fetch"])
def test_optional_article_post_is_aborted_once_without_transport_or_failure(body, resource_type):
    guard, page, _ = reading_guard()
    guard.counts["article_navigation"] = 1
    request = algorithm_list(guard, page, body=body, resource_type=resource_type)
    guard.route(request)
    assert request.aborted == ["blockedbyclient"] and not request.fetched and not request.delivered
    assert guard.failure is None and guard.phase == "reading"
    assert guard.counts["blocked_algorithm_list"] == 1
    assert guard.counts[READING_PATH] == guard.counts["reading_token"] == 0
    # A changed/duplicate startup does not acquire another tolerated attempt.
    duplicate = algorithm_list(guard, page)
    guard.route(duplicate)
    assert duplicate.aborted and not duplicate.fetched and not duplicate.delivered
    assert guard.failure.reason == "unexpected_write"
    assert guard.failure.blocked_request["endpoint"] == "algorithm_list"
    assert guard.failure.blocked_request["phase"] == "reading"
    assert guard.counts["blocked_algorithm_list"] == 1


@pytest.mark.parametrize("case", [
    "query", "fragment", "empty_query", "body", "http", "port", "cdn", "other_host",
    "get", "head", "options", "navigation", "other_frame", "other_page", "missing_frame",
    "other_article", "checkin", "loading", "inspect", "submit", "no_navigation", "no_article",
])
def test_algorithm_list_exception_requires_exact_article_scope(case):
    guard, page, _ = reading_guard()
    guard.counts["article_navigation"] = 1
    route = algorithm_list(guard, page)
    request = route.request
    if case == "query": request.url += "?private=SYNTHETIC_PRIVATE"
    elif case == "fragment": request.url += "#SYNTHETIC_PRIVATE"
    elif case == "empty_query": request.url += "?"
    elif case == "body": request.post_data = "private=SYNTHETIC_PRIVATE"
    elif case == "http": request.url = request.url.replace("https:", "http:")
    elif case == "port": request.url = JOINQUANT_ORIGIN+":444"+ALGORITHM_LIST_PATH
    elif case == "cdn": request.url = "https://cdn.joinquant.com"+ALGORITHM_LIST_PATH
    elif case == "other_host": request.url = "https://untrusted.invalid"+ALGORITHM_LIST_PATH
    elif case in {"get", "head", "options"}: request.method = case.upper()
    elif case == "navigation": request.resource_type = "document"
    elif case == "other_frame": request.frame = SimpleNamespace(page=page, url=guard.article_url)
    elif case == "other_page": request.frame = SimpleNamespace(page=object(), url=guard.article_url)
    elif case == "missing_frame": del request.frame
    elif case == "other_article": page.main_frame.url = JOINQUANT_ORIGIN+"/view/community/detail/99999"
    elif case == "checkin": guard.task = "checkin"
    elif case in {"loading", "inspect", "submit"}: guard.phase = case
    elif case == "no_navigation": guard.counts["article_navigation"] = 0
    elif case == "no_article": guard.article_url = None
    guard.route(route)
    assert route.aborted and not route.fetched and not route.delivered
    assert guard.failure.reason == "unexpected_write"
    assert guard.counts["blocked_algorithm_list"] == 0
    assert "SYNTHETIC_PRIVATE" not in json.dumps(guard.failure.blocked_request)


def test_unknown_write_remains_fatal_after_optional_post_and_first_failure_survives_cleanup():
    guard, page, _ = reading_guard()
    guard.counts["article_navigation"] = 1
    guard.route(algorithm_list(guard, page))
    unknown = Route(Reply(), url=JOINQUANT_ORIGIN+"/SYNTHETIC_PRIVATE?SYNTHETIC_QUERY=secret#SYNTHETIC_FRAGMENT")
    unknown.request.frame = page.main_frame
    guard.route(unknown)
    first = guard.failure
    assert unknown.aborted and not unknown.fetched
    assert first.reason == "unexpected_write"
    assert first.blocked_request == {
        "method": "POST", "origin": "official", "endpoint": "unknown", "phase": "reading",
        "resource_type": "fetch", "frame": "main_task", "has_query": True, "has_fragment": True,
        "path": "/:x",
    }
    later = algorithm_list(guard, page)
    guard.route(later)
    guard.close()
    assert guard.failure is first and first.blocked_request["phase"] == "reading"
    assert later.aborted and not later.fetched
    with pytest.raises(FlowError) as failure:
        guard.check()
    assert failure.value is first
    assert "SYNTHETIC" not in json.dumps(first.blocked_request)


def test_diagnostic_uses_fixed_labels_without_reading_request_payload_or_headers():
    guard, page, _ = reading_guard()
    class PrivateRequest:
        url = "https://SYNTHETIC_PRIVATE.invalid/SYNTHETIC_PRIVATE?q=SYNTHETIC_PRIVATE"
        method = "SYNTHETIC_PRIVATE"
        resource_type = "SYNTHETIC_PRIVATE"
        frame = SimpleNamespace(page=page)
        @property
        def headers(self): pytest.fail("diagnostics must not read headers")
        @property
        def post_data(self): pytest.fail("diagnostics must not read payloads")
    route = Route(Reply())
    route.request = PrivateRequest()
    guard.route(route)
    value = guard.failure.blocked_request
    assert value["origin"] == value["method"] == value["resource_type"] == "other"
    assert value["endpoint"] == "unknown"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(value)
    assert route.aborted and not route.fetched


def test_first_request_failure_reaches_keeper_cli_and_jsonl_after_gate_cleanup():
    guard, page, _ = reading_guard()
    class Gateway:
        def inspect(self, now):
            route = Route(Reply(), url=JOINQUANT_ORIGIN+"/private-SYNTHETIC_PRIVATE")
            route.request.frame = page.main_frame
            guard.route(route)
            guard.check()
        def close(self): guard.close()
    gateway = Gateway()
    gateway.guard = guard
    class Browser:
        @contextmanager
        def open(self, **_kwargs): yield page
    now = local(11, 9)
    context = KeeperContext("reading", "synthetic", SETTINGS, CapabilityRegistry({"browser": Browser()}),
                            StaticSecrets(AssertionError("no credentials")), MemoryStorage(), now)
    result = JoinQuantBrowserKeeper(page_factory=lambda *_a, **_kw: gateway).keep(context)
    assert result.status is KeepStatus.FAILED
    assert result.metadata["stage_counts"]["claim"] == 0
    assert "automation_paused" not in result.metadata
    assert result.metadata["blocked_request"]["phase"] == "reading" and guard.phase == "stopped"
    record = SimpleNamespace(result=result, started_at=now, business_target=None, clock_regressed=False, finished_at=now, run_id="synthetic", flame_id="reading", account_id="synthetic")
    output = _outcome_dict(SimpleNamespace(record=record))
    stream = StringIO()
    JsonlNotifier(stream).notify("task_failed", notification_id="synthetic", flame_id="reading",
        account_id="synthetic", result=result, occurred_at=now)
    assert json.loads(stream.getvalue())["metadata"]["blocked_request"] == output["metadata"]["blocked_request"]
    assert "SYNTHETIC_PRIVATE" not in json.dumps(output) + stream.getvalue()


@pytest.mark.parametrize("path, expected", [
    ("/default/index/GetSquareList", "/default/index/GetSquareList"),
    ("/community/post/NeedSubmitCode", "/community/post/NeedSubmitCode"),
    ("/view/community/detail/81756", "/view/community/detail/:id"),
    ("/user/12345/SYNTHETIC_PRIVATE", "/user/:id/:x"),
    ("/a1b2c3d4e5f6/token", "/:x/token"),
    ("/x" * 10, "/x/x/x/x/x/x/x/x/..."),
    ("/" + "a" * 41, "/:x"),
    ("/", "/"),
])
def test_unknown_official_write_records_only_sanitized_bare_path(path, expected):
    guard, page, _ = reading_guard()
    route = Route(Reply(), url=JOINQUANT_ORIGIN+path+"?SYNTHETIC_QUERY=secret#SYNTHETIC_FRAGMENT")
    route.request.post_data = "SYNTHETIC_BODY=secret"
    route.request.frame = page.main_frame
    guard.route(route)
    value = guard.failure.blocked_request
    assert guard.failure.reason == "unexpected_write" and route.aborted and not route.fetched
    assert value["path"] == expected and value["endpoint"] == "unknown"
    dumped = json.dumps(value)
    assert "SYNTHETIC" not in dumped and "secret" not in dumped and "81756" not in dumped


def test_non_official_origin_records_no_path():
    guard, page, _ = reading_guard()
    route = Route(Reply(), url="https://untrusted.invalid/default/index/GetSquareList")
    route.request.frame = page.main_frame
    guard.route(route)
    assert guard.failure.blocked_request["path"] is None
