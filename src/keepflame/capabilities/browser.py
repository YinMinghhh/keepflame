"""Owned browser sessions; never discover ports or attach to a user's browser."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
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
    return path


class IsolatedBrowser:
    @contextmanager
    def open(self, *, profile_dir: str, account_id: str, origin: str, headless: bool = True):
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
                yield page
            finally:
                if context is not None:
                    context.close()
