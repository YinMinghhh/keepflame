"""Process-owned browser contexts with short-lived task pages.

Contexts are never shared across accounts or processes. Login and later runs
use the same context, so no cookie export or cross-process handoff is needed.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from threading import get_ident
from urllib.parse import urlsplit

from keepflame.capabilities.browser import BrowserUnavailable, BrowserRestoreFailed, IsolatedBrowser, ProfileNotAuthorized


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
            return slot
        if len(self._contexts) >= self.max_accounts:
            raise BrowserUnavailable("browser account limit reached")
        try:
            # A visible request wins over a headless pool. JoinQuant asks for that.
            launch_headless = False if visible else self.headless
            owned = self._stack.enter_context(self.browser.session(
                profile_dir=profile_dir, account_id=account_id, origin=origin, headless=launch_headless))
            self.launch_count += 1
        except Exception as exc:
            if isinstance(exc, BrowserRestoreFailed) or getattr(self.browser, 'restore_session', False):
                self.close(reason="restore_startup_failed")
            raise
        context = owned.page.context
        slot = _Context(context, identity, permit_page=getattr(owned, 'permit_page', None))
        self._contexts[profile_dir] = slot

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
            page = slot.context.new_page()
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
            except Exception:
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
        except Exception:
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
