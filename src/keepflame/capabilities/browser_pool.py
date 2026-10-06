"""Process-owned browser contexts with short-lived task pages.

Contexts are never shared across accounts or processes. Login and later runs
use the same context, so no cookie export or cross-process handoff is needed.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from threading import get_ident
from urllib.parse import urlsplit

from keepflame.capabilities.browser import BrowserUnavailable, IsolatedBrowser, ProfileNotAuthorized


@dataclass
class _Context:
    context: object
    identity: tuple[str, str, str]
    busy: bool = False
    task_page: object | None = None
    login_page: object | None = None
    login_handler: object | None = None


class BrowserPool:
    def __init__(self, browser=None, *, headless=False, max_accounts=8):
        self.browser = browser or IsolatedBrowser()
        self.headless = headless
        self.max_accounts = max_accounts
        self._stack = ExitStack()
        self._contexts = {}
        self._thread = get_ident()
        self._closed = False

    def _get(self, profile_dir, account_id, origin):
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
        owned = self._stack.enter_context(self.browser.session(
            profile_dir=profile_dir, account_id=account_id, origin=origin, headless=self.headless))
        context = owned.page.context
        slot = _Context(context, identity)
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
            self.close()
            raise BrowserUnavailable("browser startup cleanup failed") from None
        return slot

    @contextmanager
    def open(self, *, profile_dir, account_id, origin, headless=True):
        slot = self._get(profile_dir, account_id, origin)
        if slot.busy or slot.login_page is not None:
            raise BrowserUnavailable("browser account is busy")
        slot.busy = True
        page = None
        try:
            page = slot.context.new_page()
            slot.task_page = page
            yield page
        finally:
            # Plugin route cleanup happens before this fallback. Always close its page.
            slot.task_page = None
            try:
                for owned_page in list(slot.context.pages):
                    owned_page.close()
            except Exception:
                self.close()
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
            if any(urlsplit(route.request.url).path.startswith(path) for path in blocked_paths):
                route.abort('blockedbyclient')
            else:
                route.fallback()

        slot.login_handler = guard
        slot.context.route('**/*', guard)
        try:
            slot.login_page = slot.context.new_page()
            slot.login_page.goto(login_url, wait_until='domcontentloaded', timeout=20_000)
        except Exception:
            self.end_login(profile_dir)
            raise

    def pump_login(self, profile_dir):
        slot = self._contexts[profile_dir]
        if slot.login_page is None or slot.login_page.is_closed():
            raise BrowserUnavailable("login window was closed")
        slot.login_page.wait_for_timeout(1)

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
            self.close()
            raise BrowserUnavailable("login cleanup failed; browser pool stopped") from None

    def close(self):
        self._closed = True
        self._stack.close()
        self._contexts.clear()
