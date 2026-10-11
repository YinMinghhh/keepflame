"""Two known UI readers and bounded failure diagnostics; no browser/network."""

import json
import time
from types import SimpleNamespace

import pytest

from keepflame.flames.joinquant.browser import JoinQuantPage
from keepflame.flames.joinquant.guard import FlowError, SIGNIN_PATH
from keepflame.flames.joinquant.keeper import SIGNIN_PAGE_URL, TASK_INFO_URL
from keepflame.flames.joinquant.task import task_from_settings
from tests.test_joinquant_guard import Reply, Route
from tests.test_joinquant_reading import SETTINGS, reading_guard


def credit_guard():
    guard, page, _ = reading_guard()
    guard.phase = "loading"
    guard.counts["navigation"] = 1
    page.main_frame.url = SIGNIN_PAGE_URL
    return guard, page


def ui_read(page, reply=None):
    route = Route(reply or Reply(), url=TASK_INFO_URL, method="GET")
    route.request.post_data = None
    route.request.frame = page.main_frame
    return route


def test_exactly_two_native_ui_readers_with_separate_keeper_budget():
    guard, page = credit_guard()
    body = b'{"code":"00000","data":{"taskList":{"post_view":{"status":0,"totalCount":"1"}}}}'
    for _ in range(2):
        route = ui_read(page, Reply(body=body))
        guard.route(route)
        assert route.delivered[0]["body"] == body
        assert route.fetched[0]["max_retries"] == route.fetched[0]["max_redirects"] == 0
    assert guard.counts["page_status"] == 2 and guard.counts["status"] == 0
    extra = ui_read(page)
    guard.route(extra)
    assert extra.aborted and not extra.fetched
    assert guard.failure.reason == "page_status_budget"
    assert guard.failure.blocked_request["endpoint"] == "task_status"
    assert guard.failure.blocked_request["phase"] == "loading"
    assert guard.counts["page_status"] == 2


@pytest.mark.parametrize("case", ["other_frame", "other_page", "no_frame", "wrong_document", "query", "empty_query",
    "body", "document", "no_navigation", "second_navigation", "inspect", "reading", "challenge", "validate", "submit", "confirm"])
def test_two_read_allowance_cannot_escape_its_exact_page_and_phase(case):
    guard, page = credit_guard()
    route = ui_read(page)
    if case == "other_frame": route.request.frame = SimpleNamespace(page=page, url=SIGNIN_PAGE_URL)
    elif case == "other_page": route.request.frame = SimpleNamespace(page=object(), url=SIGNIN_PAGE_URL)
    elif case == "no_frame": del route.request.frame
    elif case == "wrong_document": page.main_frame.url += "#changed"
    elif case == "query": route.request.url += "?private=SYNTHETIC_PRIVATE"
    elif case == "empty_query": route.request.url += "?"
    elif case == "body": route.request.post_data = "SYNTHETIC_PRIVATE"
    elif case == "document": route.request.resource_type = "document"
    elif case == "no_navigation": guard.counts["navigation"] = 0
    elif case == "second_navigation": guard.counts["navigation"] = 2
    else: guard.phase = case
    guard.route(route)
    assert route.aborted and not route.fetched and not route.delivered
    assert guard.counts["page_status"] == guard.counts["status"] == 0
    assert guard.counts["blocked_page_status"] == 1


def test_callback_refreshes_are_blocked_without_replacing_native_confirmation():
    guard, page = credit_guard()
    guard.phase = "confirm"
    for _ in range(102):
        route = ui_read(page)
        guard.route(route)
        assert route.aborted and not route.fetched
    assert guard.failure is None and guard.counts["page_status"] == guard.counts["status"] == 0
    assert guard.counts["blocked_page_status"] == 100  # Saturating diagnostic only.


@pytest.mark.parametrize("status", [302, 403, 429])
def test_second_ui_response_refusal_stops_without_retry_or_body_delivery(status):
    guard, page = credit_guard()
    guard.route(ui_read(page))
    refused = ui_read(page, Reply(status=status, headers={"retry-after": "172800"}))
    guard.route(refused)
    assert refused.aborted and not refused.delivered and len(refused.fetched) == 1
    assert guard.failure.reason == "page_status_http" and guard.failure.status == status
    assert guard.failure.retry_after == "172800"
    assert guard.counts["page_status"] == 2 and guard.counts["status"] == 0
    later = ui_read(page)
    guard.route(later)
    assert later.aborted and not later.fetched


@pytest.mark.parametrize("value", [0, 1, 3, "SYNTHETIC_PRIVATE", True, -1, 4, None, {}])
def test_diagnostic_copies_only_valid_completed_checks_without_browser_access(value):
    class NoBrowser:
        def __getattribute__(self, name):
            pytest.fail("failure diagnostics must not access the browser")
    page = object.__new__(JoinQuantPage)
    page.page = NoBrowser()
    page.deadline = time.monotonic() + 60
    page._entry_observations = {"card_count": value, "claim_enabled": False, "PRIVATE": "SYNTHETIC_PRIVATE"}
    page._diagnose_reading_entry()
    assert page.reading_entry["available"] is False
    assert page.reading_entry["reason"] == "post_failure_read_skipped"
    observed = page.reading_entry["observed"]
    assert ("card_count" in observed) is (type(value) is int and 0 <= value <= 3)
    assert observed["claim_enabled"] is False
    assert "SYNTHETIC_PRIVATE" not in json.dumps(page.reading_entry)


class _Expect:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_checkin_open_admits_task_info_reads_before_leaving_loading():
    """The official components fetch task info after DOMContentLoaded."""
    delivered = []

    class Surface:
        def __init__(self):
            self.url = SIGNIN_PAGE_URL
            self.context = SimpleNamespace(route=lambda *_args, **_kwargs: None, pages=[])
            self.main_frame = SimpleNamespace(page=None, url=SIGNIN_PAGE_URL)

        def goto(self, url, wait_until, timeout):
            assert url == SIGNIN_PAGE_URL
            assert wait_until == "domcontentloaded" and timeout > 0
            return SimpleNamespace(status=200, header_value=lambda _name: None)

        def wait_for_timeout(self, _milliseconds):
            if len(delivered) >= 2:
                return
            route = ui_read(self)
            self.owner.guard.route(route)
            delivered.append(route)
            if len(delivered) == 2:
                extra = ui_read(self)
                self.owner.guard.route(extra)
                delivered.append(extra)

    surface = Surface()
    page = JoinQuantPage(surface, deadline=time.monotonic() + 30, task=task_from_settings({"task": "checkin"}))
    surface.main_frame.page = surface
    surface.owner = page
    page.guard.counts["navigation"] = 1
    page.open()
    assert page.guard.phase == "loading"
    assert page.guard.failure is None
    assert page.guard.counts["page_status"] == 2
    assert page.guard.counts["blocked_page_status"] == 1
    assert page.guard.counts[SIGNIN_PATH] == 0
    assert delivered[0].delivered and delivered[1].delivered
    assert delivered[2].aborted and not delivered[2].fetched
    print("observation checkin_task_info_admitted_while_loading page_status=2")
    print("observation checkin_extra_task_info_does_not_stop=true claim=0")


def test_checkin_clicks_once_after_two_task_info_reads_and_one_button():
    """The official page draws 签到 only after its two task-info reads."""
    clicks = []

    class Surface:
        def __init__(self):
            self.context = SimpleNamespace(route=lambda *_args, **_kwargs: None, pages=[])
            self.main_frame = SimpleNamespace(page=self, url=SIGNIN_PAGE_URL)
            self.owner = None
            request = SimpleNamespace(
                url="https://www.joinquant.com/common/verifyCode/captchar",
                method="POST",
            )
            self.response = SimpleNamespace(
                status=200,
                request=request,
                body=lambda: b"not-json",
                header_value=lambda _name: None,
            )

        def get_by_role(self, role, *, name, exact):
            assert (role, name, exact) == ("button", "签到", True)
            return self

        def count(self):
            return 1 if self.owner.guard.counts["page_status"] >= 2 else 0

        def click(self, timeout):
            clicks.append({
                "page_status": self.owner.guard.counts["page_status"],
                "button_count": self.count(),
                "timeout": timeout,
            })

        def wait_for_timeout(self, _milliseconds):
            self.owner.guard.route(ui_read(self))

        def expect_request_finished(self, _matches, timeout):
            assert timeout > 0
            return _Expect(self.response.request)

        def expect_response(self, _predicate, timeout):
            assert timeout > 0
            return _Expect(self.response)

    surface = Surface()
    page = JoinQuantPage(surface, deadline=time.monotonic() + 30)
    surface.owner = page
    page.guard.counts["navigation"] = 1
    with pytest.raises(FlowError, match="challenge_shape"):
        page.challenge()
    assert clicks == [{"page_status": 2, "button_count": 1, "timeout": clicks[0]["timeout"]}]
    assert page.guard.counts["page_status"] == 2
    assert page.guard.counts[SIGNIN_PATH] == 0
    budget, budget_page = credit_guard()
    budget.task = "checkin"
    for _ in range(2):
        admitted = ui_read(budget_page)
        budget.route(admitted)
        assert admitted.delivered and not admitted.aborted
    extra = ui_read(budget_page)
    budget.route(extra)
    assert extra.aborted and not extra.fetched
    assert budget.failure is None
    assert budget.counts["page_status"] == 2
    assert budget.counts["blocked_page_status"] == 1
    assert budget.counts[SIGNIN_PATH] == 0
    print("observation checkin_withheld_until page_status=2 button_count=1 clicks=1")
    print("observation checkin_extra_task_info_ignored claim=0")
