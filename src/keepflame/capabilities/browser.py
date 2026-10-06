"""Owned browser sessions; never discover ports or attach to a user's browser."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit


class BrowserUnavailable(RuntimeError):
    pass


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

    def __init__(self, page, *, profile_dir: str, account_id: str, origin: str):
        self._page = page
        self._identity = (profile_dir, account_id, origin)
        self._active = True
        self._claimed = False

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

        def route_request(route):
            target = urlsplit(route.request.url)
            if target.scheme == "https" and target.port in (None, 443) and (
                target.hostname == host or target.hostname == "cdn." + base_domain
            ):
                route.continue_()
            else:
                route.abort("blockedbyclient")

        with sync_playwright() as playwright:
            context = None
            session = None
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), channel="chrome", headless=headless,
                    chromium_sandbox=True, timeout=20_000,
                    accept_downloads=False, service_workers="block",
                    args=["--disable-background-networking", "--disable-component-update"],
                )
                context.set_default_timeout(5_000)
                context.route("**/*", route_request)
                context.route_web_socket("**/*", lambda socket: socket.close())
                page = context.new_page()
                session = OwnedBrowserSession(page, profile_dir=str(profile), account_id=account_id, origin=origin)
                yield session
            finally:
                if session is not None:
                    session._expire()
                if context is not None:
                    context.close()
