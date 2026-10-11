"""Opt-in Chrome, original synthetic page timers, every response fulfilled locally."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.models import KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from keepflame.flames.joinquant.guard import ARTICLE_INFO_PATH, READING_PATH, SIGNIN_PATH, TOKEN_PATH
from tests.auto_images import fixtures
from tests.credit_entry_fixture import credits_document
from tests.helpers import StaticSecrets
from tests.test_joinquant_reading import ARTICLE, SETTINGS, JournalStorage
from tests.test_orchestrator import local


pytestmark = pytest.mark.skipif(os.environ.get("KEEPFLAME_SYNTHETIC_BROWSER") != "1", reason="opt-in isolated Chrome test")


SUCCESS_CASES = {"read_then_claim", "already_claimable", "child_first", "string_status"}
ENTRY_FAILURES = {"missing_status", "completed_status", "changed_text", "disabled_button", "hidden_button",
                  "duplicate_button", "duplicate_card", "wrong_callback", "third_status", "credits_unknown_write",
                  "page_403", "page_429", "page_10001", "cdp_unavailable"}


@pytest.mark.parametrize("case", ["read_then_claim", "already_claimable", "unconfirmed", "wrong_rule", "paid_article",
    "other_article", "unknown_startup_write", "child_first", "string_status", *sorted(ENTRY_FAILURES)])
def test_reading_original_page_and_claim_gates(case):
    from playwright.sync_api import sync_playwright
    payload, piece, expected = fixtures()
    remote = {"qualified": case not in {"read_then_claim", "paid_article", "other_article", "unknown_startup_write"}, "complete": False}
    page_reads = 0
    requests = []
    synthetic_entry = {}
    storage = JournalStorage()
    post_id = "99999" if case == "other_article" else "12345"
    article_html = '''<!doctype html><meta charset="utf-8"><title>Synthetic article</title>
      <article>This text is synthetic. No real article or account is used.</article><script>
      // The official created() hook starts this optional POST before reading.
      const request = new XMLHttpRequest();
      request.open('POST', '/algorithm/index/runList');
      request.onerror = () => { window.algorithmListBlocked = true; };
      request.send();
      EXTRA_STARTUP
      fetch('/community/post/detailV2?postId=12345');
      setTimeout(async () => {
        await fetch('/user/account/getToken');
        await fetch('/community/post/postView', {method:'POST',
          headers:{'Content-Type':'application/x-www-form-urlencoded'},
          body:new URLSearchParams({postId:'POST_ID',appKey:'synthetic',appSecret:'synthetic'})});
      }, 6000);
      </script>'''.replace("POST_ID", post_id).replace("EXTRA_STARTUP",
        "fetch('/synthetic/unexpected-write', {method:'POST'}).catch(()=>{});" if case == "unknown_startup_write" else "")
    credits_html = credits_document(piece, case)

    def backend(request):
        nonlocal page_reads
        parsed = urlsplit(request.url)
        assert parsed.scheme == "https" and parsed.netloc == "www.joinquant.com"
        requests.append((request.method, parsed.path))
        if parsed.path == "/view/community/detail/12345":
            assert storage.reservations == 1
            return 200, {"content-type": "text/html"}, article_html.encode()
        if parsed.path == "/view/user/floor":
            return 200, {"content-type": "text/html"}, credits_html.encode()
        if parsed.path == "/credits/task/getTaskInfo":
            task = ({"status": 1, "totalCount": 0} if remote["complete"] else
                    {"status": 0, "totalCount": 1} if remote["qualified"] else {})
            if getattr(request, "resource_type", None) in {"xhr", "fetch"}:
                page_reads += 1
                if page_reads == 2:
                    if case == "missing_status": task = {}
                    elif case == "completed_status": task = {"status": 1}
                    elif case == "string_status": task = {"status": "0", "totalCount": "1"}
                    elif case in {"page_403", "page_429"}:
                        return int(case[-3:]), {"retry-after": "172800"}, b""
                    elif case == "page_10001":
                        return 200, {"content-type": "application/json"}, b'{"code":"10001"}'
            body = {"code": "00000", "data": {"taskList": {"post_view": task}}}
        elif parsed.path == ARTICLE_INFO_PATH:
            body = {"code": "00000", "data": {"type": 5 if case == "paid_article" else 0}}
        elif parsed.path == READING_PATH:
            assert parse_qs(request.post_data)["postId"] == ["12345"]
            remote["qualified"] = True
            body = {"code": "00000"}
        elif parsed.path == TOKEN_PATH:
            body = {"code": "00000", "data": {"synthetic": True}}
        elif parsed.path == "/common/verifyCode/captchar":
            assert storage.reservations == 1
            body = payload
        elif parsed.path == "/common/verifyCode/validate":
            assert parse_qs(request.post_data)["axisX"] == [str(expected[0])]
            body = {"data": {"result": True, "token": "synthetic-only"}}
        elif parsed.path == SIGNIN_PATH:
            assert parse_qs(request.post_data)["ruleKey"] == ["post_view"]
            remote["complete"] = case != "unconfirmed"
            body = {"code": "00000"}
        else:
            raise AssertionError("unapproved synthetic request")
        return 200, {"content-type": "application/json"}, json.dumps(body).encode()

    def route_backend(route):
        status, headers, body = backend(route.request)
        route.fulfill(status=status, headers=headers, body=body)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True, timeout=20_000,
            args=["--host-resolver-rules=MAP * ~NOTFOUND", "--disable-background-networking", "--disable-component-update"])
        try:
            session = browser.new_context(service_workers="block", accept_downloads=False)
            session.route("**/*", route_backend)
            page = session.new_page()
            @contextmanager
            def open_page(**kwargs):
                yield page
            context = KeeperContext("reading", "synthetic", SETTINGS,
                CapabilityRegistry({"browser": type("Browser", (), {"open": staticmethod(open_page)})()}),
                StaticSecrets(AssertionError("no credentials")), storage, local(11, 9))
            class ObservedPage(JoinQuantPage):
                def close(self):
                    synthetic_entry["reading_buttons"] = self.page.locator("#reading-card button").all_text_contents()
                    super().close()
            if case == "cdp_unavailable":
                session.new_cdp_session = lambda *_: pytest.fail("failed page must not be queried over CDP")
            result = JoinQuantBrowserKeeper(page_factory=lambda p, **kw: ObservedPage(
                p, request_fetch=lambda source, **opts: backend(source.request), **kw)).keep(context)
            assert page.is_closed()
            session.new_page().close()
            counts = result.metadata["stage_counts"]
            artifacts = os.environ.get("KEEPFLAME_BROWSER_ARTIFACTS")
            if artifacts:
                target = Path(artifacts); target.mkdir(parents=True, exist_ok=True)
                action = storage.get_action("joinquant", "synthetic", "reading")
                (target / f"reading-{case}.json").write_text(json.dumps({
                    "case": case, "status": result.status.value, "metadata": result.metadata,
                    "requests": requests, "action": vars(action) if action else None,
                    "synthetic_entry": synthetic_entry, "external_requests": 0, "fixture": "official-contract-reconstruction",
                }, indent=2, default=str) + "\n")
            assert counts["answer"] <= 1 and counts["claim"] <= 1 and counts["page_status"] <= 2
            assert ("POST", "/algorithm/index/runList") not in requests
            assert "synthetic-only" not in json.dumps(result.metadata)
            if case in SUCCESS_CASES:
                assert result.status is KeepStatus.SUCCESS and remote["complete"] is True
                assert counts["claim"] == counts["answer"] == counts["challenge"] == 1
                assert counts["page_status"] == 2
                assert counts["status"] == (3 if case == "read_then_claim" else 2)
                assert result.receipt is not None
                assert storage.get_action("joinquant", "synthetic", "reading").state == "confirmed"
                assert counts["reading_registration"] == (case == "read_then_claim")
                assert counts["blocked_algorithm_list"] == (case == "read_then_claim")
            else:
                assert result.status is not KeepStatus.SUCCESS
                if counts["claim"] == 0 and not result.metadata.get("pause_account"):
                    assert "automation_paused" not in result.metadata
                else:
                    assert result.metadata["automation_paused"]
            if case in ENTRY_FAILURES:
                assert counts["reading_registration"] == counts["answer"] == counts["claim"] == 0
                assert counts["challenge"] == (1 if case == "wrong_callback" else 0)
                assert result.receipt is None and result.reward is None
                if storage.get_action("joinquant", "synthetic", "reading") is not None:
                    assert storage.get_action("joinquant", "synthetic", "reading").state == "pending"
                assert "SYNTHETIC_PRIVATE" not in json.dumps(result.metadata)
                if case == "cdp_unavailable":
                    assert result.metadata["reason"] == "reading_button_changed"
                    assert result.metadata["reading_entry"] == {
                        "available": False, "reason": "post_failure_read_skipped", "observed": {"card_count": 1}}
                if case in {"missing_status", "completed_status", "changed_text", "disabled_button", "hidden_button", "duplicate_button", "duplicate_card"}:
                    entry = result.metadata["reading_entry"]
                    assert entry["available"] is False and entry["reason"] == "post_failure_read_skipped"
                    observed = entry["observed"]
                    assert observed["card_count"] == (2 if case == "duplicate_card" else 1)
                    if case in {"missing_status", "completed_status", "changed_text", "hidden_button", "duplicate_card"}:
                        assert "claim_count" not in observed  # No extra read after the failed check.
                    if case == "disabled_button": assert observed["claim_enabled"] is False
                    if case == "duplicate_button": assert observed["claim_count"] == 2
                if case == "third_status":
                    assert result.metadata["reason"] == "page_status_budget"
                    assert counts["page_status"] == 2 and counts["blocked_page_status"] == 1
                if case == "credits_unknown_write":
                    assert result.metadata["reason"] == "unexpected_write"
                    assert ("POST", "/synthetic/unexpected-write") not in requests
                if case in {"page_403", "page_429"}:
                    assert result.metadata["remote_status"] == int(case[-3:])
                    assert result.next_action_at == local(13, 9)
                    assert counts["status"] == 1 and page_reads == 2
                if case == "page_10001": assert result.metadata["reason"] == "page_status_changed"
            if case == "wrong_rule":
                assert ("POST", SIGNIN_PATH) not in requests
            if case in {"paid_article", "other_article"}:
                assert ("POST", READING_PATH) not in requests and counts["answer"] == counts["claim"] == 0
            if case == "unconfirmed":
                assert result.metadata["reason"] == "claim_unconfirmed"
                assert storage.get_action("joinquant", "synthetic", "reading").state == "pending"
            if case == "unknown_startup_write":
                assert ("POST", "/synthetic/unexpected-write") not in requests
                assert result.metadata["reason"] == "unexpected_write"
                assert result.metadata["blocked_request"]["endpoint"] == "unknown"
                assert result.metadata["blocked_request"]["phase"] == "reading"
                assert counts["blocked_algorithm_list"] == 1
                assert counts["reading_registration"] == counts["challenge"] == counts["claim"] == 0
                assert storage.get_action("joinquant", "synthetic", "reading").state == "pending"
        finally:
            browser.close()
