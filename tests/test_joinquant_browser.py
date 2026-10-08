"""Opt-in real Chrome test against route-fulfilled synthetic pages only."""

from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("KEEPFLAME_SYNTHETIC_BROWSER") != "1", reason="opt-in isolated Chrome test")

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.models import KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from tests.helpers import MemoryStorage, StaticSecrets
from tests.test_orchestrator import local


HTML = '''<!doctype html><meta charset="utf-8"><title>KeepFlame synthetic sign-in</title>
<h1>KeepFlame — synthetic account only</h1><p id="state">NOT SIGNED IN</p>
<button id="signin">签到</button>
<div id="yth_captchar" style="display:none"><div id="xy_img" style="width:56px;height:56px"></div></div>
<script>
const button = document.querySelector('#signin');
button.addEventListener('click', async () => {
  await fetch('/common/verifyCode/captchar', {method:'POST'});
  document.querySelector('#xy_img').style.backgroundImage = `url("PIECE")`;
  document.querySelector('#yth_captchar').style.display = 'block';
});
button.__vue__ = {getToken: async value => {
  const result = await fetch('/credits/task/receiveCredits', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:new URLSearchParams({ruleKey:'login',valideCode:value,appKey:'synthetic-key',appSecret:'synthetic-secret'})});
  if (result.ok) {
    document.querySelector('#state').textContent='BACKEND CONFIRMED';
    document.querySelector('#yth_captchar').style.display='none';
  }
}};
</script>'''


@pytest.mark.parametrize("case", ["complete", "unconfirmed", "challenge_429", "validation_429", "callback_changed", "expired"])
def test_real_chrome_automatic_flow_without_external_io(tmp_path, case):
    complete = case == "complete"
    from playwright.sync_api import sync_playwright
    from tests.auto_images import fixtures
    payload, piece, expected = fixtures()
    html = HTML.replace("button.__vue__", "button.__oldvue__") if case == "callback_changed" else HTML
    records = []
    remote = {"signed":False}
    unexpected = []

    def route_request(route):
        from urllib.parse import urlsplit, parse_qs
        request = route.request
        parsed = urlsplit(request.url)
        records.append((request.method, parsed.path))
        assert parsed.hostname == "www.joinquant.com"
        if parsed.path == "/view/user/floor":
            return route.fulfill(status=200, content_type="text/html", body=html.replace('PIECE',piece))
        if parsed.path == "/credits/task/getTaskInfo":
            if case == "expired":
                return route.fulfill(status=401, body="synthetic expired session")
            return route.fulfill(json={"code":"00000","data":{"taskList":{"login":{"status":int(remote["signed"])}}}})
        if parsed.path == "/common/verifyCode/captchar":
            if case == "challenge_429":
                return route.fulfill(status=429, headers={"Retry-After":"86400"}, body="synthetic limit")
            return route.fulfill(json=payload)
        if parsed.path == "/common/verifyCode/validate":
            if case == "validation_429":
                return route.fulfill(status=429, headers={"Retry-After":"172800"}, body="synthetic limit")
            answer = parse_qs(request.post_data or "").get('axisX')
            assert answer == [str(expected[0])]
            return route.fulfill(json={"data":{"result":True,"token":"synthetic-ephemeral-value"}})
        if parsed.path == "/credits/task/receiveCredits":
            assert parse_qs(request.post_data)["valideCode"] == ["synthetic-ephemeral-value"]
            remote["signed"] = complete
            return route.fulfill(json={"code":"00000"})
        unexpected.append(parsed.path)
        route.abort("blockedbyclient")

    def request_fetch(source, *, timeout):
        class MemoryRoute:
            request = source.request
            def fulfill(self, *, status=200, headers=None, content_type=None, body=None, json=None):
                import json as jsonlib
                result_headers = dict(headers or {})
                if json is not None:
                    body = jsonlib.dumps(json)
                    content_type = "application/json"
                if content_type:
                    result_headers["content-type"] = content_type
                return status, {k.lower():v for k,v in result_headers.items()}, body.encode() if isinstance(body,str) else body or b""
            def abort(self, *_):
                raise AssertionError("unexpected synthetic native request")
        return route_request(MemoryRoute())

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True, chromium_sandbox=True, timeout=20_000,
            args=["--host-resolver-rules=MAP * ~NOTFOUND", "--disable-background-networking", "--disable-component-update"],
        )
        try:
            session = browser.new_context(service_workers="block", accept_downloads=False)
            session.route("**/*", route_request)
            page = session.new_page()

            class Factory:
                @contextmanager
                def open(self, **options):
                    yield page

            context = KeeperContext(
                flame_id="synthetic-auto", account_id="synthetic",
                settings={"browser_profile_dir":"unused-synthetic-only"},
                capabilities=CapabilityRegistry({"browser":Factory()}),
                secrets=StaticSecrets(AssertionError("no credentials allowed")),
                storage=MemoryStorage(), now=local(11,9),
            )
            result = JoinQuantBrowserKeeper(page_factory=lambda page, deadline, date_fence: JoinQuantPage(page, deadline=deadline, request_fetch=request_fetch, date_fence=date_fence)).keep(context)
            expected_status = KeepStatus.SUCCESS if complete else KeepStatus.WAITING_HUMAN if case in ("expired", "unconfirmed") else KeepStatus.FAILED
            assert result.status is expected_status
            if complete:
                assert result.metadata["automatic_action"] is True
                assert remote["signed"] is True
            else:
                assert result.metadata["reason"] == {
                    "unconfirmed":"claim_unconfirmed", "challenge_429":"challenge_http",
                    "validation_429":"validation_http", "callback_changed":"site_callback_changed",
                    "expired":"status_http",
                }[case]
            if case in ("challenge_429", "validation_429"):
                assert result.next_action_at == local(12 if case == "challenge_429" else 13, 9)
                assert records.count(("GET","/credits/task/getTaskInfo")) == 1
            assert records.count(("POST","/common/verifyCode/captchar")) == (0 if case == "expired" else 1)
            assert records.count(("POST","/common/verifyCode/validate")) == (1 if case in ("complete","unconfirmed","validation_429") else 0)
            assert "synthetic-ephemeral-value" not in json.dumps(result.metadata)
            assert not unexpected
            assert page.is_closed()
            session.new_page().close()  # The account context survives a task.
            artifacts = os.environ.get("KEEPFLAME_BROWSER_ARTIFACTS")
            if artifacts:
                target = Path(artifacts)
                target.mkdir(parents=True,exist_ok=True)
                (target / f"synthetic-{case}.json").write_text(json.dumps({"status":result.status.value,"metadata":result.metadata,"requests":records,"external_requests":0},indent=2)+'\n')
        finally:
            browser.close()
