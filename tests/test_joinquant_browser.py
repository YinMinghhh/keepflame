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
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper
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
  const result = await fetch('/synthetic/complete', {method:'POST', body:value});
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
        if parsed.path == "/synthetic/complete":
            assert request.post_data == "synthetic-ephemeral-value"
            remote["signed"] = complete
            return route.fulfill(status=200 if complete else 500, body="synthetic response")
        unexpected.append(parsed.path)
        route.abort("blockedbyclient")

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
            result = JoinQuantBrowserKeeper().keep(context)
            expected_status = KeepStatus.SUCCESS if complete else KeepStatus.WAITING_HUMAN if case == "expired" else KeepStatus.FAILED
            assert result.status is expected_status
            if complete:
                assert result.metadata["automatic_action"] is True
                assert page.locator('#state').inner_text() == "BACKEND CONFIRMED"
            else:
                assert result.metadata["reason"] == {
                    "unconfirmed":"completion_unconfirmed", "challenge_429":"challenge_rejected",
                    "validation_429":"validation_http", "callback_changed":"site_callback_changed",
                    "expired":"session_refresh_required",
                }[case]
            if case in ("challenge_429", "validation_429"):
                assert result.next_action_at == local(12 if case == "challenge_429" else 13, 9)
                assert records.count(("GET","/credits/task/getTaskInfo")) == 1
            assert records.count(("POST","/common/verifyCode/captchar")) == (0 if case == "expired" else 1)
            assert records.count(("POST","/common/verifyCode/validate")) == (1 if case in ("complete","unconfirmed","validation_429") else 0)
            assert "synthetic-ephemeral-value" not in json.dumps(result.metadata)
            assert not unexpected
            artifacts = os.environ.get("KEEPFLAME_BROWSER_ARTIFACTS")
            if artifacts:
                target = Path(artifacts)
                target.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(target / f"synthetic-{case}.png"))
                (target / f"synthetic-{case}.json").write_text(json.dumps({"status":result.status.value,"metadata":result.metadata,"requests":records,"external_requests":0},indent=2)+'\n')
        finally:
            browser.close()
