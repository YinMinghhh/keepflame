"""Optional failure evidence cannot add I/O, extend a deadline or outlive a task."""

import asyncio
from contextlib import contextmanager
import threading
import time

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper, JoinQuantPage
from keepflame.flames.joinquant.guard import SignInGuard
from types import SimpleNamespace
from tests.helpers import StaticSecrets
from tests.test_joinquant_reading import Gateway, JournalStorage, SETTINGS, reading
from tests.test_orchestrator import local


class ForbiddenBrowser:
    def __getattribute__(self, name):
        raise AssertionError("failure evidence must not touch any browser property or API")


def snapshot(deadline):
    gateway = object.__new__(JoinQuantPage)
    gateway.page = ForbiddenBrowser()
    gateway.deadline = deadline
    gateway._entry_observations = {"card_count": 1}
    return gateway


@pytest.mark.parametrize("remaining", [-30, 0, .001, 1, 90])
def test_optional_diagnostic_does_not_extend_or_wait_for_the_attempt_budget(remaining):
    deadline = time.monotonic() + remaining
    gateway = snapshot(deadline)
    before = threading.enumerate()
    started = time.monotonic()
    gateway._diagnose_reading_entry()
    elapsed = time.monotonic() - started
    assert elapsed < .1
    assert gateway.deadline == deadline
    assert threading.enumerate() == before
    assert gateway.reading_entry == {
        "available": False,
        "reason": "task_budget_exhausted" if remaining <= 0 else "post_failure_read_skipped",
        "observed": {"card_count": 1},
    }


def test_cancelled_task_has_no_diagnostic_children_and_preserves_cancellation():
    async def scenario():
        ready = asyncio.Event()
        snapshots = []
        async def work():
            try:
                ready.set()
                await asyncio.Future()
            finally:
                before = asyncio.all_tasks()
                gateway = snapshot(time.monotonic() + 10)
                gateway._diagnose_reading_entry()
                assert asyncio.all_tasks() == before
                snapshots.append(gateway.reading_entry)
        before = asyncio.all_tasks()
        task = asyncio.create_task(work())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.all_tasks() == before
        assert task.cancelled() and snapshots[0]["reason"] == "post_failure_read_skipped"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [asyncio.CancelledError, KeyboardInterrupt])
def test_cancel_after_reservation_closes_gateway_and_keeps_pending(failure):
    storage = JournalStorage()
    closed = []
    class CancelledGateway(Gateway):
        def challenge(self):
            assert self.storage.reservations == 1
            snapshot(time.monotonic() + 10)._diagnose_reading_entry()
            raise failure()
        def close(self):
            closed.append("gateway")
    gateway = CancelledGateway([reading({"status": 0, "totalCount": 1})], storage)
    class Browser:
        @contextmanager
        def open(self, **_):
            try:
                yield object()
            finally:
                closed.append("owned_page")
    context = KeeperContext("reading", "synthetic", SETTINGS,
        CapabilityRegistry({"browser": Browser()}), StaticSecrets(AssertionError("no credentials")), storage, local(11, 9))
    with pytest.raises(failure):
        JoinQuantBrowserKeeper(page_factory=lambda *_a, **_kw: gateway).keep(context)
    assert closed == ["gateway", "owned_page"]
    assert storage.reservations == 1 and storage.get_action("joinquant", "synthetic", "reading").state == "pending"
    assert gateway.answers == gateway.reads == 0


@pytest.mark.parametrize("case", ["normal", "already_closed", "close_during_unroute", "unroute_error"])
def test_guard_only_skips_cleanup_after_this_context_has_closed(case):
    listeners = {}
    calls = []
    class Context:
        def route(self, *_): pass
        def on(self, event, callback): listeners[event] = callback
        def remove_listener(self, event, callback):
            assert listeners.pop(event) == callback
        def unroute(self, *_):
            calls.append("unroute")
            if case == "close_during_unroute": listeners["close"]()
            if case in {"close_during_unroute", "unroute_error"}: raise RuntimeError("synthetic cleanup failure")
    guard = SignInGuard(SimpleNamespace(context=Context()), deadline=time.monotonic() + 10)
    if case == "already_closed": listeners["close"]()
    if case == "unroute_error":
        with pytest.raises(RuntimeError, match="synthetic cleanup failure"): guard.close()
    else:
        guard.close()
    guard.close()  # Idempotent, including listener cleanup.
    assert calls == ([] if case == "already_closed" else ["unroute"])
    assert guard.phase == "stopped" and not listeners
