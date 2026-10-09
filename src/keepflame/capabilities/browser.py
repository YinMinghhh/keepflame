"""Owned browser sessions; never discover ports or attach to a user's browser."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit


class BrowserUnavailable(RuntimeError):
    pass


class BrowserRestoreFailed(BrowserUnavailable):
    """A failed restore invalidates its owner; never silently launch it again."""


class ProfileNotAuthorized(BrowserUnavailable):
    pass


def validate_profile(reference: str, account_id: str) -> Path:
    path = Path(reference)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ProfileNotAuthorized("a dedicated absolute browser profile is required")
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ProfileNotAuthorized("browser profiles must be outside repositories")
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ProfileNotAuthorized("browser profile must be owned by this user with mode 0700")
    marker = path / ".keepflame-profile.json"
    if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 1024:
        raise ProfileNotAuthorized("browser profile has not been explicitly enrolled")
    if marker.stat().st_uid != os.getuid() or marker.stat().st_mode & 0o077:
        raise ProfileNotAuthorized("profile enrollment must have mode 0600")
    try:
        data = json.loads(marker.read_text())
    except (OSError, ValueError) as exc:
        raise ProfileNotAuthorized("invalid browser profile enrollment") from exc
    if data != {"version": 1, "account_id": account_id, "purpose": "keepflame"}:
        raise ProfileNotAuthorized("browser profile belongs to a different account or purpose")
    # Inspect metadata only; never open browser databases or follow data links.
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ProfileNotAuthorized("browser profile parent must be controlled by this user")
    pending = [path]
    inspected = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                inspected += 1
                if inspected > 50_000:
                    raise ProfileNotAuthorized("browser profile metadata limit")
                if entry.is_symlink():
                    if directory == path and entry.name in {"SingletonLock", "SingletonSocket", "SingletonCookie"}:
                        continue
                    raise ProfileNotAuthorized("browser data must not contain symbolic links")
                metadata = entry.stat(follow_symlinks=False)
                if metadata.st_uid != os.getuid():
                    raise ProfileNotAuthorized("browser data must belong to this user")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                    raise ProfileNotAuthorized("browser data must not contain hard-linked files")
    return path


class OwnedBrowserSession:
    """One in-process handoff from an IsolatedBrowser-owned login session."""

    def __init__(self, page, *, profile_dir: str, account_id: str, origin: str, permit_page=None):
        self._page = page
        self._identity = (profile_dir, account_id, origin)
        self._active = True
        self._claimed = False
        self._permit_page = permit_page

    def permit_page(self, page):
        """Admit the pool's newly created task/login page, never restored tabs."""
        if not self._active or page.context is not self._page.context:
            raise BrowserUnavailable("page does not belong to the owned context")
        if self._permit_page is not None:
            self._permit_page(page)

    @property
    def page(self):
        if not self._active:
            raise BrowserUnavailable("owned browser session has ended")
        return self._page

    @contextmanager
    def open(self, *, profile_dir: str, account_id: str, origin: str, headless: bool = True):
        if not self._active or self._claimed:
            raise BrowserUnavailable("owned browser session is no longer available")
        if (profile_dir, account_id, origin) != self._identity:
            raise ProfileNotAuthorized("owned browser session identity does not match")
        # The session's original display mode is retained; no browser is restarted.
        self._claimed = True
        yield self._page

    def _expire(self):
        self._active = False


class IsolatedBrowser:
    def __init__(self, *, restore_session: bool = False):
        self.restore_session = restore_session

    @contextmanager
    def open(self, *, profile_dir: str, account_id: str, origin: str, headless: bool = True):
        options = dict(profile_dir=profile_dir, account_id=account_id, origin=origin, headless=headless)
        with self.session(**options) as session:
            with session.open(**options) as page:
                yield page

    @contextmanager
    def session(self, *, profile_dir: str, account_id: str, origin: str, headless: bool = True):
        """Keep an owned browser open across human login and one Keeper run.

        A user data directory preserves persistent cookies, but not necessarily
        session cookies after Chrome exits. Pass this handle as the runtime's
        browser capability while still inside this context; never export cookies.
        """
        profile = validate_profile(profile_dir, account_id)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable("install the browser optional dependencies first") from exc
        # The requested HTTPS host and its CDN are the only allowed page destinations.
        host = urlsplit(origin).hostname
        if urlsplit(origin).scheme != "https" or not host:
            raise BrowserUnavailable("browser origin must be HTTPS")
        base_domain = host.removeprefix("www.")
        admitted_page = None

        def permit_page(page):
            nonlocal admitted_page
            admitted_page = page

        def route_request(route):
            if self.restore_session:
                # A late request from a restored/closed page or worker must not
                # reach the Node transport after startup cleanup has finished.
                try:
                    request_page = route.request.frame.page
                    if request_page is not admitted_page or request_page.is_closed():
                        route.abort("blockedbyclient")
                        return
                except Exception:
                    route.abort("blockedbyclient")
                    return
            target = urlsplit(route.request.url)
            if target.scheme == "https" and target.port in (None, 443) and (
                target.hostname == host or target.hostname == "cdn." + base_domain
            ):
                response = None
                try:
                    # Browser redirects can bypass subsequent route callbacks. Do not
                    # let a permitted URL redirect around the origin or login gate.
                    response = route.fetch(timeout=10_000, max_redirects=0, max_retries=0)
                    if 300 <= response.status < 400:
                        route.abort("blockedbyclient")
                    else:
                        route.fulfill(response=response)
                except Exception:
                    route.abort("blockedbyclient")
                finally:
                    if response is not None:
                        response.dispose()
            else:
                route.abort("blockedbyclient")

        from keepflame.capabilities.browser_restore import restore_barrier, restore_deadline, require_bounded_driver

        if self.restore_session:
            try:
                require_bounded_driver()
            except Exception:
                raise BrowserRestoreFailed("unsupported browser restore driver") from None

        barrier = restore_barrier() if self.restore_session else nullcontext([])
        # The barrier encloses launch AND close. A route installed after launch
        # cannot stop restored pages from making startup requests by itself.
        with barrier as restore_args, sync_playwright() as playwright:
            context = None
            session = None
            try:
                startup = restore_deadline(playwright, 20) if self.restore_session else nullcontext()
                with startup:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile), channel="chrome", headless=headless,
                        chromium_sandbox=True, timeout=20_000,
                        accept_downloads=False, service_workers="block",
                        args=["--disable-background-networking", "--disable-component-update", *restore_args],
                    )
                    context.set_default_timeout(5_000)
                    if self.restore_session:
                        def deny_startup(route):
                            route.abort("blockedbyclient")

                        context.route("**/*", deny_startup)
                        context.route_web_socket("**/*", lambda socket: socket.close())
                        try:
                            for restored_page in list(context.pages):
                                restored_page.close()
                        except Exception:
                            raise BrowserUnavailable("restored page cleanup failed") from None
                        # Every old page is closed before the forwarding route can
                        # be enabled. The Chrome proxy barrier stays in place.
                        context.unroute("**/*", deny_startup)
                    context.route("**/*", route_request)
                    context.route_web_socket("**/*", lambda socket: socket.close())
                    page = context.new_page()
                    permit_page(page)
                    session = OwnedBrowserSession(page, profile_dir=str(profile), account_id=account_id,
                                                  origin=origin, permit_page=permit_page)
            except Exception:
                if self.restore_session:
                    raise BrowserRestoreFailed("browser restore startup failed") from None
                raise
            else:
                yield session
            finally:
                if session is not None:
                    session._expire()
                if context is not None:
                    try:
                        cleanup = restore_deadline(playwright, 5) if self.restore_session else nullcontext()
                        with cleanup:
                            context.close()
                    except Exception:
                        if self.restore_session:
                            raise BrowserRestoreFailed("browser restore cleanup failed") from None
                        raise
