"""Regression probes for page-generated writes; all backend responses are synthetic."""

from contextlib import contextmanager
import inspect
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.models import KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from tests.helpers import MemoryStorage, StaticSecrets
from tests.test_orchestrator import local

pytestmark = pytest.mark.skipif(os.environ.get("KEEPFLAME_SYNTHETIC_BROWSER") != "1", reason="opt-in isolated Chrome test")
SIGNIN = "/credits/task/receiveCredits"


@pytest.mark.parametrize("case", ["callback_scope", "callback_429", "callback_403", "wrong_rule", "duplicate_signin", "redirect", "unknown_endpoint", "spoofed_fetch"])
def test_page_callback_writes_are_scoped_and_response_is_observed(case):
    from playwright.sync_api import sync_playwright
    from tests.auto_images import fixtures
    payload, piece, expected = fixtures()
    requests = []
    remote = {"signed":False}
    extra = ""
    if case == "callback_scope":
        extra = """await fetch('/common/verifyCode/validate', {method:'POST',body:'axisX=0'}).catch(()=>{});
        await fetch('/synthetic/unrelated-mutation', {method:'POST'}).catch(()=>{});"""
    rule = "not_login" if case == "wrong_rule" else "login"
    endpoint = "/synthetic/unrelated-mutation" if case == "unknown_endpoint" else SIGNIN
    submit = f"await fetch('{endpoint}', options).catch(()=>{{}});"
    if case == "duplicate_signin":
        submit = f"await Promise.all([fetch('{endpoint}',options).catch(()=>{{}}),fetch('{endpoint}',options).catch(()=>{{}})]);"
    if case == "spoofed_fetch":
        submit += "window.fetch=async()=>new Response(JSON.stringify({code:'00000',data:{taskList:{login:{status:1}}}}),{status:200});"
    html = f'''<!doctype html><meta charset="utf-8"><h1>Synthetic callback boundary</h1>
    <button id="signin">签到</button><div id="yth_captchar" style="display:none"><div id="xy_img" style="width:56px;height:56px"></div></div>
    <script>
    const button=document.querySelector('#signin');
    button.addEventListener('click',async()=>{{
        await fetch('/common/verifyCode/captchar',{{method:'POST'}});
        document.querySelector('#xy_img').style.backgroundImage='url("{piece}")';
        document.querySelector('#yth_captchar').style.display='block';
    }});
    button.__vue__={{getToken:async value=>{{
        {extra}
        const options={{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
            body:new URLSearchParams({{ruleKey:'{rule}',valideCode:value,appKey:'synthetic-key',appSecret:'synthetic-secret'}})}};
        {submit}
    }}}};
    </script>'''

    def backend(request):
        path=urlsplit(request.url).path
        requests.append((request.method,path))
        status=200;headers={"content-type":"application/json"}
        if path=="/view/user/floor":
            return 200,{"content-type":"text/html"},html.encode()
        if path=="/credits/task/getTaskInfo":
            body={"code":"00000","data":{"taskList":{"login":{"status":int(remote["signed"])}}}}
        elif path=="/common/verifyCode/captchar":
            body=payload
        elif path=="/common/verifyCode/validate":
            body={"data":{"result":True,"token":"synthetic-only-token"}}
        elif path in (SIGNIN,"/synthetic/unrelated-mutation"):
            if path==SIGNIN and case in ("callback_429","callback_403"):
                status=int(case[-3:]);headers["retry-after"]="172800"
            elif path==SIGNIN and case=="redirect":
                status=307;headers["location"]="/synthetic/unrelated-mutation"
            else:
                remote["signed"]=case != "spoofed_fetch"
            body={"code":"00000"}
        else:
            raise AssertionError("unexpected synthetic request")
        return status,headers,json.dumps(body).encode()

    def route_backend(route):
        status,headers,body=backend(route.request)
        route.fulfill(status=status,headers=headers,body=body)

    # Before the fix, ordinary route fulfillment reproduces the vulnerable flow.
    # After the fix, this transport replaces only the guard's no-redirect fetch.
    def request_fetch(route, *, timeout):
        return backend(route.request)

    def gateway(page, *, deadline):
        options={"deadline":deadline}
        if "request_fetch" in inspect.signature(JoinQuantPage).parameters:
            options["request_fetch"]=request_fetch
        return JoinQuantPage(page,**options)

    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel="chrome",headless=True,chromium_sandbox=True,
            args=["--host-resolver-rules=MAP * ~NOTFOUND","--disable-background-networking","--disable-component-update"])
        try:
            session=browser.new_context(service_workers="block",accept_downloads=False)
            session.route("**/*",route_backend)
            page=session.new_page()
            class Factory:
                @contextmanager
                def open(self,**options):
                    yield page
            context=KeeperContext(flame_id="synthetic",account_id="synthetic",settings={"browser_profile_dir":"unused"},
                capabilities=CapabilityRegistry({"browser":Factory()}),secrets=StaticSecrets(AssertionError("no credentials")),
                storage=MemoryStorage(),now=local(11,9))
            result=JoinQuantBrowserKeeper(page_factory=gateway).keep(context)
            artifacts=os.environ.get("KEEPFLAME_BROWSER_ARTIFACTS")
            if artifacts:
                target=Path(artifacts);target.mkdir(parents=True,exist_ok=True)
                (target/f"guard-{case}.json").write_text(json.dumps({"case":case,"status":result.status.value,
                    "metadata":result.metadata,"next_action_at":result.next_action_at.isoformat() if result.next_action_at else None,
                    "requests":requests,"external_requests":0},indent=2)+'\n')
            assert requests.count(("POST","/common/verifyCode/validate"))==1
            assert ("POST","/synthetic/unrelated-mutation") not in requests
            assert requests.count(("POST",SIGNIN))<=1
            assert result.status is not KeepStatus.SUCCESS
            assert requests.count(("GET","/credits/task/getTaskInfo"))==(4 if case == "spoofed_fetch" else 1)
            if case in ("callback_429","callback_403"):
                assert result.metadata["remote_status"]==int(case[-3:])
                assert result.next_action_at==local(13,9)
                assert result.status is (KeepStatus.WAITING_HUMAN if case=="callback_403" else KeepStatus.FAILED)
            assert "synthetic-only-token" not in json.dumps(result.metadata)
        finally:
            browser.close()
