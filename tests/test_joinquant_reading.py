"""Public reading-task contract and durable action policy; no external I/O."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import time
from zoneinfo import ZoneInfo

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.models import KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from keepflame.flames.joinquant.guard import ARTICLE_INFO_PATH, READING_PATH, SIGNIN_PATH, TOKEN_PATH, SignInGuard
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN, JoinQuantKeeper
from keepflame.flames.joinquant.matching import Match
from keepflame.flames.joinquant.task import TaskConfigurationError, task_from_settings
from tests.helpers import MemoryStorage, StaticSecrets, response
from tests.test_joinquant_guard import Reply, Route
from tests.test_orchestrator import local


ARTICLE = JOINQUANT_ORIGIN + "/view/community/detail/12345"
SETTINGS = {"task": "reading", "article_url": ARTICLE, "reading_wait_seconds": 30,
            "browser_profile_dir": "/unused-synthetic", "mode": "browser"}


def reading(record=...):
    tasks = {} if record is ... else {"post_view": record}
    return JoinQuantKeeper._interpret(response(200, json.dumps({"code": "00000", "data": {"taskList": tasks}})),
                                     now=local(11, 9), task="reading")


@pytest.mark.parametrize("record", [..., {}, {"totalCount": 0}])
def test_reading_absence_is_not_claimable(record):
    result = reading(record)
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.metadata["reading_state"] == "needs_reading"
    assert result.human_action.kind == "READ_OFFICIAL_ARTICLE"


@pytest.mark.parametrize("value", [None, True, False, [], {}, 0.0, 1.0, 2, "01", " 0"])
def test_reading_rejects_ambiguous_status(value):
    result = reading({"status": value, "totalCount": 1})
    assert result.status is KeepStatus.FAILED and result.metadata["reading_state"] == "unknown"


@pytest.mark.parametrize("value", [None, True, False, [], {}, -1, 1.0, "-1", "01", " 1", "١"])
def test_claim_requires_safe_nonnegative_count(value):
    assert reading({"status": 0, "totalCount": value}).status is KeepStatus.FAILED


def test_reading_container_null_missing_and_claimable_are_distinct():
    assert reading().metadata["status_shape"]["post_view"] == "missing"
    assert reading(None).metadata["status_shape"]["post_view"] == "null"
    assert reading({"status": 0}).status is KeepStatus.FAILED
    for value in (0, "0"):
        result = reading({"status": value, "totalCount": "0"})
        assert result.metadata["reading_state"] == "claimable"
    assert reading({"status": 1}).status is KeepStatus.SUCCESS
    assert reading({"status": "1"}).status is KeepStatus.SUCCESS


def test_reading_duplicate_members_reject_before_classification():
    raw = '{"code":"00000","data":{"taskList":{"post_view":{"status":0,"status":1,"totalCount":1}}}}'
    result = JoinQuantKeeper._interpret(response(200, raw), now=local(11, 9), task="reading")
    assert result.metadata == {"error_type": "protocol", "protocol_reason": "duplicate_json_member", "reading_state": "unknown"}


@pytest.mark.parametrize("status,body,account", [(401, "", True), (403, "denied", True),
    (200, '{"code":"10001"}', True), (429, "", False), (200, '{"code":"00000","data":null}', False)])
def test_inspect_normalization_pauses_auth_rate_limit_and_protocol(status, body, account):
    result = JoinQuantKeeper._inspection_result(JoinQuantKeeper._interpret(response(status, body), now=local(11, 9)))
    assert result.metadata["automation_paused"] is True
    assert result.metadata.get("pause_account", False) is account


@pytest.mark.parametrize("url", [None, "", ARTICLE+"?type=5", ARTICLE+"#payment-content", ARTICLE+"/", ARTICLE.replace("https", "http"),
                                 ARTICLE.replace("www.joinquant.com", "other.invalid"), ARTICLE.replace("12345", "abc"), ARTICLE+"%2F"])
def test_article_selection_is_explicit_exact_and_without_payment_query(url):
    with pytest.raises(TaskConfigurationError):
        task_from_settings({**SETTINGS, "article_url": url})


@pytest.mark.parametrize("wait", [29, 121, True, 30.0, "30"])
def test_elapsed_wait_is_bounded_and_not_coerced(wait):
    with pytest.raises(TaskConfigurationError):
        task_from_settings({**SETTINGS, "reading_wait_seconds": wait})


class JournalStorage(MemoryStorage):
    def __init__(self):
        super().__init__()
        self.actions = {}
        self.reservations = 0

    def get_action(self, keeper, account_id, task):
        return self.actions.get((keeper, account_id, task))

    def reserve_action(self, keeper, account_id, task, day, now):
        key = keeper, account_id, task
        previous = self.actions.get(key)
        if previous and (previous.state == "pending" or previous.day == day):
            return False
        self.actions[key] = SimpleNamespace(state="pending", day=day, started_at=now, confirmed_at=None)
        self.reservations += 1
        return True

    def confirm_action(self, keeper, account_id, task, now, *, day):
        item = self.actions[(keeper, account_id, task)]
        if item.state != "pending" or item.day != day:
            return False
        item.state = "confirmed"
        item.confirmed_at = now
        return True


class Gateway:
    def __init__(self, results, storage):
        self.results, self.storage = iter(results), storage
        self.checks = self.reads = self.claims = self.answers = self.opens = self.closes = 0
    def inspect(self, now):
        self.checks += 1
        return next(self.results)
    def open(self):
        self.opens += 1
    def read_article(self):
        assert self.storage.reservations == 1
        self.reads += 1
    def challenge(self):
        assert self.storage.reservations == 1
        self.claims += 1
        return {}, "synthetic"
    def submit(self, x):
        self.answers += 1
    def close(self):
        self.closes += 1


def execute_reading(results, *, storage=None, now=None, solver=lambda *_: Match(90, 0, .99, .4)):
    storage = storage or JournalStorage()
    gateway = Gateway(results, storage)
    @contextmanager
    def browser(**kwargs):
        yield object()
    context = KeeperContext("reading", "synthetic", SETTINGS, CapabilityRegistry({"browser": SimpleNamespace(open=browser)}),
                            StaticSecrets(AssertionError("no credentials")), storage, now or local(11, 9))
    result = JoinQuantBrowserKeeper(page_factory=lambda *a, **kw: gateway, solver=solver, sleeper=lambda _: None).keep(context)
    assert gateway.closes == 1
    return result, gateway, storage


def test_normal_read_then_qualified_claim_requires_backend_completion():
    result, page, storage = execute_reading([reading(), reading({"status": 0, "totalCount": 1}), reading({"status": 1})])
    assert result.status is KeepStatus.SUCCESS
    assert page.reads == page.claims == page.answers == page.opens == 1
    assert page.checks == 3 and storage.reservations == 1
    assert storage.get_action("joinquant", "synthetic", "reading").state == "confirmed"


def test_pending_cross_day_never_confirms_reads_or_claims():
    first, _, storage = execute_reading([reading(), reading()])
    assert first.status is KeepStatus.WAITING_HUMAN and first.metadata["reason"] == "claim_unconfirmed"
    result, page, _ = execute_reading([reading({"status": 0, "totalCount": 1})], storage=storage, now=local(12, 9))
    assert result.metadata["automation_paused"] is True and result.metadata["reason"] == "claim_unconfirmed"
    assert page.reads == page.claims == page.answers == page.opens == 0 and page.checks == 1
    result, page, _ = execute_reading([reading({"status": 1})], storage=storage, now=local(12, 10))
    assert result.status is KeepStatus.WAITING_HUMAN and page.claims == 0
    assert result.metadata["unconfirmed_reason"] == "pending_different_day"
    assert result.metadata["reading_state"] == "complete"
    assert storage.get_action("joinquant", "synthetic", "reading").day == local(11, 9).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert storage.get_action("joinquant", "synthetic", "reading").state == "pending"
    result, page, _ = execute_reading([reading({"status": 0, "totalCount": 1})], storage=storage, now=local(12, 11))
    assert result.metadata["unconfirmed_reason"] == "pending_different_day" and page.claims == page.reads == 0


def test_claim_ack_without_completion_is_paused_and_not_retried():
    eligible = reading({"status": 0, "totalCount": 2})
    result, page, storage = execute_reading([eligible]*4)
    assert result.status is KeepStatus.WAITING_HUMAN and result.metadata["reason"] == "claim_unconfirmed"
    assert page.claims == page.answers == 1 and page.checks == 4
    assert storage.get_action("joinquant", "synthetic", "reading").state == "pending"


def test_auth_failure_pauses_account_without_read_or_claim():
    expired = JoinQuantKeeper._interpret(response(401, ""), now=local(11, 9), task="reading")
    result, page, storage = execute_reading([expired])
    assert result.metadata["automation_paused"] and result.metadata["pause_account"]
    assert storage.reservations == page.reads == page.claims == 0


def reading_guard():
    removed = []
    page = SimpleNamespace(context=SimpleNamespace(route=lambda *_: None, unroute=lambda *args: removed.append(args)))
    page.main_frame = SimpleNamespace(page=page, url=ARTICLE)
    guard = SignInGuard(page, deadline=time.monotonic()+60, task="reading")
    guard.phase = "inspect"
    guard.begin_reading(article_id="12345", article_url=ARTICLE)
    return guard, page, removed


def natural_registration(guard, page, *, body=None):
    route = Route(Reply(), url=JOINQUANT_ORIGIN+READING_PATH,
                  body=body or "postId=12345&appKey=synthetic&appSecret=synthetic")
    route.request.frame = page.main_frame
    guard.route(route)
    return route


def test_reading_registration_requires_known_public_article_and_one_natural_page_request():
    guard, page, _ = reading_guard()
    detail = Route(Reply(body=b'{"code":"00000","data":{"type":0}}'),
                   url=JOINQUANT_ORIGIN+ARTICLE_INFO_PATH+"?postId=12345", method="GET")
    guard.route(detail)
    assert guard.article_allowed and detail.delivered
    token = Route(Reply(), url=JOINQUANT_ORIGIN+TOKEN_PATH, method="GET")
    guard.route(token)
    assert token.delivered
    registration = natural_registration(guard, page)
    assert registration.delivered and len(registration.fetched) == 1
    repeated = natural_registration(guard, page)
    assert repeated.aborted and not repeated.fetched


@pytest.mark.parametrize("body", ["postId=999&appKey=x&appSecret=y", "postId=12345&appKey=x&appSecret=y&elapsed=6",
                                  "postId=12345&postId=12345&appKey=x&appSecret=y"])
def test_registration_cannot_change_article_or_include_fabricated_telemetry(body):
    guard, page, _ = reading_guard()
    guard.article_allowed = True
    guard.counts["reading_token"] = 1
    attempt = natural_registration(guard, page, body=body)
    assert attempt.aborted and not attempt.fetched


@pytest.mark.parametrize("kind", [5, "5", True, None, 6])
def test_paid_or_unknown_article_type_blocks_registration(kind):
    guard, _, _ = reading_guard()
    route = Route(Reply(body=json.dumps({"code": "00000", "data": {"type": kind}}).encode()),
                  url=JOINQUANT_ORIGIN+ARTICLE_INFO_PATH+"?postId=12345", method="GET")
    guard.route(route)
    assert route.aborted and not guard.article_allowed


def test_task_rule_is_fixed_and_gate_cleanup_does_not_remove_other_handlers():
    guard, _, removed = reading_guard()
    guard.phase = "submit"
    wrong = Route(Reply())  # Default is ruleKey=login.
    guard.route(wrong)
    assert wrong.aborted and not wrong.fetched
    guard.close()
    guard.close()
    assert removed == [("**/*", guard._handler)]


def test_ui_status_forwarding_is_native_and_not_a_completion_confirmation():
    guard, _, _ = reading_guard()
    guard.phase = "loading"
    route = Route(Reply(body=b'{"code":"00000","data":{"taskList":{"post_view":{"status":1}}}}'),
                  url=JOINQUANT_ORIGIN+"/credits/task/getTaskInfo", method="GET")
    guard.route(route)
    assert route.delivered and guard.counts["page_status"] == 1 and guard.counts["status"] == 0
    assert route.fetched[0]["max_redirects"] == route.fetched[0]["max_retries"] == 0
    assert guard.signin_response is None
