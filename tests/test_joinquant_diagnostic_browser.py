"""Opt-in real Chrome failure exits; no account, platform or external responses."""

import asyncio
import json
import os
from pathlib import Path
import time

import pytest

from keepflame.flames.joinquant.browser import JoinQuantPage
from keepflame.flames.joinquant.guard import FlowError
from keepflame.flames.joinquant.task import task_from_settings
from tests.test_joinquant_reading import SETTINGS

pytestmark = pytest.mark.skipif(os.environ.get("KEEPFLAME_SYNTHETIC_BROWSER") != "1", reason="opt-in isolated Chrome test")


@pytest.mark.parametrize("case", ["renderer_busy", "cdp_disconnected", "page_closed", "context_closed",
                                  "cancelled", "budget_exhausted", "near_budget"])
def test_optional_evidence_never_delays_failed_page_cleanup(case):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True, timeout=15_000,
            args=["--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=http://127.0.0.1:9",
                  "--proxy-bypass-list=<-loopback>", "--disable-background-networking", "--disable-component-update"])
        context = browser.new_context(service_workers="block", accept_downloads=False)
        escaped = []
        def deny(route):
            escaped.append(True)
            route.abort("blockedbyclient")
        context.route("**/*", deny)
        context.route_web_socket("**/*", lambda ws: ws.close())
        page = context.new_page()
        gateway = JoinQuantPage(page, deadline=time.monotonic() + 10, task=task_from_settings(SETTINGS))
        html = '<div class="floor-credit-card"><span class="header-title">浏览社区文章</span><button>去看看</button></div>'
        if case == "renderer_busy":
            html += '<script>setTimeout(()=>{while(true){}},200)</script>'
        page.set_content(html, timeout=1_000)
        if case == "cdp_disconnected":
            session = context.new_cdp_session(page)
            session.detach()  # An actual detached session, no untrusted port/attachment.
        if case == "page_closed": page.close()
        if case == "context_closed": context.close()
        if case in {"near_budget", "budget_exhausted"}:
            gateway.deadline = gateway.guard.deadline = time.monotonic() + (.03 if case == "near_budget" else -1)
        deadline = gateway.deadline
        # A regression to any new synchronous CDP call must fail this test.
        context.new_cdp_session = lambda *_: pytest.fail("failure evidence must not open CDP")
        started = time.monotonic()
        cancelled = False
        try:
            if case == "cancelled":
                async def cancel_then_record():
                    before = asyncio.all_tasks()
                    ready = asyncio.Event()
                    async def work():
                        try:
                            ready.set()
                            await asyncio.Future()
                        finally:
                            active = asyncio.all_tasks()
                            gateway._diagnose_reading_entry()
                            assert asyncio.all_tasks() == active
                    task = asyncio.create_task(work())
                    await ready.wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError): await task
                    assert task.cancelled() and asyncio.all_tasks() == before
                # Private sync bridge is used only by this cancellation probe.
                page._sync(cancel_then_record())
                cancelled = True
                reason = "cancelled"
            else:
                with pytest.raises(FlowError) as caught:
                    gateway._button()
                reason = caught.value.reason
                if case in {"near_budget", "budget_exhausted"}:
                    assert reason == "time_budget"
                    # Evidence recording after budget exhaustion still makes no API call.
                    gateway._diagnose_reading_entry()
                else:
                    assert reason == ("reading_card_changed" if case in {"page_closed", "context_closed"} else "reading_button_changed")
            assert gateway.deadline == deadline
            assert gateway.reading_entry["available"] is False
            assert gateway.reading_entry["reason"] == ("task_budget_exhausted" if case in {"near_budget", "budget_exhausted"} else "post_failure_read_skipped")
        finally:
            gateway.close()
            context.close()
            browser.close()
        elapsed = time.monotonic() - started
        assert elapsed < (8 if case in {"renderer_busy", "cdp_disconnected"} else 2)
        assert page.is_closed() and not escaped
        directory = os.environ.get("KEEPFLAME_BROWSER_ARTIFACTS")
        if directory:
            output = Path(directory); output.mkdir(parents=True, exist_ok=True)
            (output/f"diagnostic-{case}.json").write_text(json.dumps({
                "case": case, "elapsed_seconds": elapsed, "reason": reason, "reading_entry": gateway.reading_entry,
                "page_closed": True, "context_and_browser_close_returned": True, "cancelled": cancelled,
                "diagnostic_cdp_calls": 0, "external_requests": 0, "deadline_unchanged": True,
            }, indent=2) + "\n")
