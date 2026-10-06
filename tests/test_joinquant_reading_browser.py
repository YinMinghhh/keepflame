"""Opt-in Chrome, original synthetic page timers, every response fulfilled locally."""

from contextlib import contextmanager
import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.models import KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from keepflame.flames.joinquant.guard import ARTICLE_INFO_PATH, READING_PATH, SIGNIN_PATH, TOKEN_PATH
from tests.auto_images import fixtures
from tests.helpers import StaticSecrets
from tests.test_joinquant_reading import ARTICLE, SETTINGS, JournalStorage
from tests.test_orchestrator import local


pytestmark = pytest.mark.skipif(os.environ.get("KEEPFLAME_SYNTHETIC_BROWSER") != "1", reason="opt-in isolated Chrome test")


@pytest.mark.parametrize("case", ["read_then_claim", "already_claimable", "unconfirmed", "wrong_rule", "paid_article", "other_article"])
def test_reading_original_page_and_claim_gates(case):
    from playwright.sync_api import sync_playwright
    payload, piece, expected = fixtures()
    remote = {"qualified": case in {"already_claimable", "unconfirmed", "wrong_rule"}, "complete": False}
    requests = []
    storage = JournalStorage()
    post_id = "99999" if case == "other_article" else "12345"
    article_html = '''<!doctype html><meta charset="utf-8"><title>Synthetic article</title>
      <article>This text is synthetic. No real article or account is used.</article><script>
      fetch('/community/post/detailV2?postId=12345');
      setTimeout(async () => {
        await fetch('/user/account/getToken');
        await fetch('/community/post/postView', {method:'POST',
          headers:{'Content-Type':'application/x-www-form-urlencoded'},
          body:new URLSearchParams({postId:'POST_ID',appKey:'synthetic',appSecret:'synthetic'})});
      }, 6000);
      </script>'''.replace("POST_ID", post_id)
    credits_html = '''<!doctype html><meta charset="utf-8"><title>Synthetic credits</title>
      <div class="floor-credit-card"><span class="header-title">Other task</span><button>立即领取</button></div>
      <div class="floor-credit-card"><span class="header-title">浏览社区文章</span><div id="actions"></div></div>
      <div id="yth_captchar" style="display:none"><div id="xy_img" style="width:56px;height:56px"></div></div>
      <script>
      fetch('/credits/task/getTaskInfo').then(r => r.json()).then(body => {
        if (body.data.taskList.post_view.status !== 0) return;
        const button = document.createElement('button'); button.textContent = '立即领取';
        document.querySelector('#actions').appendChild(button);
        button.addEventListener('click', async () => {
          await fetch('/common/verifyCode/captchar', {method:'POST'});
          document.querySelector('#xy_img').style.backgroundImage = 'url("PIECE")';
          document.querySelector('#yth_captchar').style.display='block';
        });
        button.__vue__ = {curRuleKey:'post_view',tokenType:'receiveCredits',getToken: async value => {
          await fetch('/user/account/getToken');
          await fetch('/credits/task/receiveCredits', {method:'POST',
            headers:{'Content-Type':'application/x-www-form-urlencoded'},
            body:new URLSearchParams({ruleKey:'RULE',valideCode:value,appKey:'synthetic',appSecret:'synthetic'})});
        }};
      });
      </script>'''.replace("PIECE", piece).replace("RULE", "login" if case == "wrong_rule" else "post_view")

    def backend(request):
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
            result = JoinQuantBrowserKeeper(page_factory=lambda p, **kw: JoinQuantPage(
                p, request_fetch=lambda source, **opts: backend(source.request), **kw)).keep(context)
            assert page.is_closed()
            session.new_page().close()
            counts = result.metadata["stage_counts"]
            assert counts["answer"] <= 1 and counts["claim"] <= 1 and counts["page_status"] <= 1
            assert "synthetic-only" not in json.dumps(result.metadata)
            if case in {"read_then_claim", "already_claimable"}:
                assert result.status is KeepStatus.SUCCESS and remote["complete"] is True
                assert counts["claim"] == counts["answer"] == 1
                assert counts["reading_registration"] == (case == "read_then_claim")
            else:
                assert result.status is not KeepStatus.SUCCESS and result.metadata["automation_paused"]
            if case == "wrong_rule":
                assert ("POST", SIGNIN_PATH) not in requests
            if case in {"paid_article", "other_article"}:
                assert ("POST", READING_PATH) not in requests and counts["answer"] == counts["claim"] == 0
            if case == "unconfirmed":
                assert result.metadata["reason"] == "claim_unconfirmed"
                assert storage.get_action("joinquant", "synthetic", "reading").state == "pending"
        finally:
            browser.close()
