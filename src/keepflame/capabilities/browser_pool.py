"""Process-owned browser contexts with short-lived task pages.

Contexts are never shared across accounts or processes. Login and later runs
use the same context, so no cookie export or cross-process handoff is needed.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from threading import get_ident
from urllib.parse import urlsplit

from keepflame.capabilities.browser import (
    BrowserDisconnected, BrowserUnavailable, BrowserRestoreFailed, IsolatedBrowser, ProfileNotAuthorized,
)

# Playwright reports a quit browser, crashed renderer or closed context with
# TargetClosedError (or a plain Error naming the closed target).
_CLOSED_MARKERS = ("has been closed", "target closed", "browser closed", "browser has disconnected",
                   "connection closed")


def _closed_error(exc):
    if type(exc).__name__ == "TargetClosedError":
        return True
    message = str(exc).split("\n", 1)[0].casefold()
    return any(marker in message for marker in _CLOSED_MARKERS)


@dataclass
class _Context:
    context: object
    identity: tuple[str, str, str]
    busy: bool = False
    task_page: object | None = None
    login_page: object | None = None
    login_handler: object | None = None
    permit_page: object | None = None
    proof_url: str | None = None
    visible: bool = False
    stack: ExitStack = field(default_factory=ExitStack)
    disconnected: bool = False


class BrowserPool:
    def __init__(self, browser=None, *, headless=False, max_accounts=8):
        self.browser = browser or IsolatedBrowser()
        self.headless = headless
        self.max_accounts = max_accounts
        self._stack = ExitStack()
        self._contexts = {}
        self._thread = get_ident()
        self._closed = False
        self.close_reason = None
        self.launch_count = 0
        self.relaunch_count = 0
        self.last_disconnect = None

    @property
    def closed(self):
        return self._closed

    def _get(self, profile_dir, account_id, origin, *, visible=False):
        if self._closed or get_ident() != self._thread:
            raise BrowserUnavailable("browser pool is closed or used from another thread")
        identity = (profile_dir, account_id, origin)
        for existing in self._contexts.values():
            if existing.identity[1:] == (account_id, origin) and existing.identity[0] != profile_dir:
                raise ProfileNotAuthorized("browser account is already bound to another profile")
        slot = self._contexts.get(profile_dir)
        if slot is not None:
            if slot.identity != identity:
                raise ProfileNotAuthorized("browser profile is already bound to another identity")
            if not slot.busy and slot.login_page is None and not self._alive(slot):
                # The dedicated Chrome was quit or crashed since the last run.
                return self._relaunch(slot, "disconnected_before_use")
            return slot
        if len(self._contexts) >= self.max_accounts:
            raise BrowserUnavailable("browser account limit reached")
        return self._launch(identity, visible=visible)

    def _launch(self, identity, *, visible):
        profile_dir, account_id, origin = identity
        slot_stack = ExitStack()
        try:
            # A visible request wins over a headless pool. JoinQuant asks for that.
            launch_headless = False if visible else self.headless
            owned = slot_stack.enter_context(self.browser.session(
                profile_dir=profile_dir, account_id=account_id, origin=origin, headless=launch_headless))
            self.launch_count += 1
        except Exception as exc:
            if isinstance(exc, BrowserRestoreFailed) or getattr(self.browser, 'restore_session', False):
                self.close(reason="restore_startup_failed")
            raise
        # Pool close also closes this slot; closing an already closed slot is a no-op.
        self._stack.push(slot_stack)
        context = owned.page.context
        slot = _Context(context, identity, permit_page=getattr(owned, 'permit_page', None),
                        visible=visible, stack=slot_stack)
        self._contexts[profile_dir] = slot
        self._watch(slot)

        def owned_page_only(route):
            # Deny idle traffic, popups and workers before the origin allowlist.
            try:
                request_page = route.request.frame.page
                allowed = request_page is (slot.login_page or slot.task_page) and not request_page.is_closed()
            except Exception:
                allowed = False
            if allowed:
                route.fallback()
            else:
                route.abort("blockedbyclient")

        context.route("**/*", owned_page_only)
        try:
            for page in list(context.pages):
                page.close()
        except Exception:
            self.close(reason="security_cleanup_failed")
            raise BrowserUnavailable("browser startup cleanup failed") from None
        return slot

    @staticmethod
    def _watch(slot):
        """Mark the slot dead when Playwright reports the context or browser closing."""
        def mark(*_args):
            slot.disconnected = True

        subscribe = getattr(slot.context, "on", None)
        if callable(subscribe):
            try:
                subscribe("close", mark)
            except Exception:
                pass
        browser = getattr(slot.context, "browser", None)
        subscribe = getattr(browser, "on", None) if browser is not None else None
        if callable(subscribe):
            try:
                subscribe("disconnected", mark)
            except Exception:
                pass

    @staticmethod
    def _alive(slot):
        if slot.disconnected:
            return False
        try:
            browser = getattr(slot.context, "browser", None)
            connected = getattr(browser, "is_connected", None) if browser is not None else None
            if callable(connected) and connected() is False:
                return False
            closed = getattr(slot.context, "is_closed", None)
            if callable(closed) and closed() is True:
                return False
        except Exception:
            return False
        return True

    def _discard(self, slot):
        """Tear down a dead slot only. Its gates die with the closed context."""
        if self._contexts.get(slot.identity[0]) is slot:
            del self._contexts[slot.identity[0]]
        slot.disconnected = True
        try:
            slot.stack.close()
        except Exception:
            # A dead Chrome cannot close cleanly; the driver still stops.
            pass

    def _relaunch(self, slot, why):
        self.last_disconnect = why
        self._discard(slot)
        if getattr(self.browser, 'restore_session', False):
            # A restored owner is never launched again silently.
            self.close(reason="browser_disconnected")
            raise BrowserDisconnected("browser disconnected; restore owner is not relaunched")
        try:
            fresh = self._launch(slot.identity, visible=slot.visible)
        except Exception as exc:
            if isinstance(exc, ProfileNotAuthorized):
                raise
            raise BrowserDisconnected("browser relaunch failed: " + type(exc).__name__) from None
        self.relaunch_count += 1
        return fresh

    def _new_page(self, slot):
        """Open a page; a dead browser is relaunched once. No request precedes this."""
        try:
            return slot, slot.context.new_page()
        except Exception as exc:
            if not (_closed_error(exc) or not self._alive(slot)):
                raise
        slot = self._relaunch(slot, "disconnected_on_new_page")
        try:
            return slot, slot.context.new_page()
        except Exception as exc:
            if _closed_error(exc) or not self._alive(slot):
                self._discard(slot)
                raise BrowserDisconnected("relaunched browser closed again") from None
            raise

    def open_visible(self, *, profile_dir, account_id, origin):
        """Open the dedicated window once. Later task pages reuse this context."""
        self._get(profile_dir, account_id, origin, visible=True)

    @contextmanager
    def open(self, *, profile_dir, account_id, origin, headless=True):
        slot = self._get(profile_dir, account_id, origin, visible=headless is False)
        if slot.busy or slot.login_page is not None:
            raise BrowserUnavailable("browser account is busy")
        slot.busy = True
        page = None
        try:
            try:
                slot, page = self._new_page(slot)
            finally:
                # A relaunch replaces the slot; mark the live one busy.
                slot.busy = True
            slot.task_page = page
            if slot.permit_page is not None:
                slot.permit_page(page)
            yield page
        finally:
            # Plugin route cleanup happens before this fallback. Always close its page.
            slot.task_page = None
            try:
                for owned_page in list(slot.context.pages):
                    owned_page.close()
            except Exception as exc:
                if _closed_error(exc) or not self._alive(slot):
                    # The whole context is gone, so no page or gate outlives it.
                    slot.busy = False
                    self._discard(slot)
                    raise BrowserDisconnected("browser closed during the task") from None
                self.close(reason="security_cleanup_failed")
                raise BrowserUnavailable("browser task cleanup failed") from None
            finally:
                slot.busy = False

    def begin_login(self, *, profile_dir, account_id, origin, login_url, blocked_paths):
        if urlsplit(login_url).scheme != 'https' or urlsplit(login_url).netloc != urlsplit(origin).netloc:
            raise BrowserUnavailable("login URL must use the enrolled origin")
        if not blocked_paths or any(not path.startswith('/') for path in blocked_paths):
            raise BrowserUnavailable("login write exclusions are required")
        slot = self._get(profile_dir, account_id, origin)
        if slot.busy or slot.login_page is not None:
            raise BrowserUnavailable("browser account is busy")

        def guard(route):
            target = urlsplit(route.request.url)
            proof = slot.proof_url
            method = getattr(route.request, "method", "")
            # One exact task-info GET proves the session. Reward routes stay blocked.
            if (isinstance(proof, str) and method == "GET" and route.request.url == proof
                    and not target.query and not target.fragment
                    and not getattr(route.request, "post_data", None)):
                route.fallback()
                return
            if any(target.path.startswith(path) for path in blocked_paths):
                route.abort('blockedbyclient')
            else:
                route.fallback()

        slot.login_handler = guard
        slot.context.route('**/*', guard)
        try:
            slot.login_page = slot.context.new_page()
            if slot.permit_page is not None:
                slot.permit_page(slot.login_page)
            slot.login_page.goto(login_url, wait_until='domcontentloaded', timeout=20_000)
        except Exception:
            self.end_login(profile_dir)
            raise

    def pump_login(self, profile_dir):
        slot = self._contexts[profile_dir]
        if slot.login_page is None or slot.login_page.is_closed():
            raise BrowserUnavailable("login window was closed")
        slot.login_page.wait_for_timeout(1)

    def login_window_open(self, profile_dir):
        slot = self._contexts.get(profile_dir)
        if slot is None or slot.login_page is None:
            return False
        try:
            return not slot.login_page.is_closed()
        except Exception:
            return False

    def read_login_proof(self, profile_dir, url):
        """Read one task-info response from the open login window.

        A test double may supply context.proof_body. The live window uses the
        page's own fetch so the session cookie stays inside that profile.
        """
        if not self.login_window_open(profile_dir):
            raise BrowserUnavailable("login window was closed")
        slot = self._contexts[profile_dir]
        provider = getattr(slot.context, "proof_body", None)
        if callable(provider):
            return provider(url)
        slot.proof_url = url
        try:
            raw = slot.login_page.evaluate(
                """async (url) => {
                    const response = await fetch(url, {
                        credentials: 'include',
                        headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
                    });
                    const text = await response.text();
                    return {status: response.status, body: text.slice(0, 1048576)};
                }""",
                url,
            )
        finally:
            slot.proof_url = None
        if not isinstance(raw, dict):
            return None
        status, body = raw.get("status"), raw.get("body")
        if type(status) is not int or not isinstance(body, str):
            return None
        return status, body.encode("utf-8")

    def end_login(self, profile_dir):
        slot = self._contexts.get(profile_dir)
        if slot is None:
            return
        try:
            for page in list(slot.context.pages):
                page.close()
            slot.login_page = None
            if slot.login_handler is not None:
                slot.context.unroute('**/*', slot.login_handler)
            slot.login_handler = None
        except Exception as exc:
            if _closed_error(exc) or not self._alive(slot):
                # The entire owned context already closed, so the gate cannot be bypassed.
                slot.login_page = None
                slot.login_handler = None
                self._discard(slot)
                raise BrowserDisconnected("browser closed during login") from None
            # Keep the gate until the entire owned context closes; never reuse it.
            self.close(reason="security_cleanup_failed")
            raise BrowserUnavailable("login cleanup failed; browser pool stopped") from None

    def close(self, *, reason="program_exit"):
        if self._closed:
            return
        self._closed = True
        self.close_reason = reason
        self._stack.close()
        self._contexts.clear()
