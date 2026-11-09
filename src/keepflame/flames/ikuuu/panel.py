"""Short-lived Chrome profile for one ikuuu origin.

Cookies stay in the enrolled profile. This module does not call the cookie
export APIs and does not open the operator's everyday Chrome profile.
A persistent context is how a later process sees cookies the browser saved.
That persistence is the browser's own store; callers must not treat a unit
double as proof that Chrome restored a remote login.
"""

from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlsplit

from keepflame.capabilities.browser import ProfileNotAuthorized, validate_profile
from keepflame.capabilities.http import HttpResponse
from keepflame.capabilities.profile_owner import exclusive_profile
from keepflame.flames.ikuuu.session import public_host

_MAX_HOSTS = 16


class PanelError(RuntimeError):
    """A browser failure whose message is a short token, never a URL or secret."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _allowed(origin_host: str, extra: tuple[str, ...]) -> set[str]:
    if len(extra) > _MAX_HOSTS:
        raise PanelError("too_many_hosts")
    allowed = {origin_host}
    for host in extra:
        if not public_host(host):
            raise PanelError("host_rejected")
        allowed.add(host)
    return allowed


def _response(response, url: str) -> HttpResponse:
    try:
        status = response.status
        headers = {key.casefold(): value for key, value in response.headers.items()
                   if key.casefold() in {"location", "hx-redirect"}}
        body = response.body()
    except Exception:
        raise PanelError("request_failed") from None
    if not isinstance(status, int) or isinstance(body, str) or len(body) > 1_048_576:
        raise PanelError("request_failed")
    return HttpResponse(status, url, headers, body[:1_048_576])


class RequestPanel:
    """Same-origin requests through the profile's cookie jar. Values are not returned."""

    def __init__(self, context, origin: str) -> None:
        self._context = context
        self._origin = origin
        self._host = urlsplit(origin).hostname

    def request(self, method: str, url: str, *, headers: dict[str, str], body: bytes = b"") -> HttpResponse:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != self._host or parts.port not in (None, 443):
            raise PanelError("off_origin")
        safe_headers = {key: value for key, value in headers.items() if key.lower() not in {"cookie", "authorization"}}
        try:
            response = self._context.request.fetch(
                url, method=method, headers=safe_headers,
                data=None if method == "GET" else body,
                max_redirects=0, timeout=15_000,
            )
        except Exception:
            raise PanelError("request_failed") from None
        try:
            return _response(response, url)
        finally:
            dispose = getattr(response, "dispose", None)
            if callable(dispose):
                try:
                    dispose()
                except Exception:
                    pass


class LoginWindow(RequestPanel):
    def __init__(self, context, origin: str) -> None:
        super().__init__(context, origin)
        self._page = None

    def open_login(self, url: str) -> None:
        if urlsplit(url).hostname != self._host:
            raise PanelError("off_origin")
        try:
            self._page = self._context.new_page()
            self._page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception:
            raise PanelError("login_window_failed") from None

    def wait_closed(self, timeout_ms: int) -> str:
        if self._page is None:
            raise PanelError("login_window_failed")
        try:
            self._page.wait_for_event("close", timeout=timeout_ms)
            return "closed"
        except Exception:
            return "timeout"


def _route(allowed: set[str]):
    def handle(route):
        target = urlsplit(route.request.url)
        method = getattr(route.request, "method", "")
        if (target.scheme != "https" or target.port not in (None, 443) or target.hostname not in allowed
                or (method == "POST" and target.path.startswith("/user/checkin"))):
            route.abort("blockedbyclient")
            return
        response = None
        try:
            response = route.fetch(timeout=15_000, max_redirects=0, max_retries=0)
            route.fulfill(response=response)
        except Exception:
            route.abort("blockedbyclient")
        finally:
            if response is not None:
                dispose = getattr(response, "dispose", None)
                if callable(dispose):
                    try:
                        dispose()
                    except Exception:
                        pass

    return handle


@contextmanager
def open_panel(*, profile_dir: str, account_id: str, origin: str,
               allowed_hosts: tuple[str, ...] = (), headless: bool = True, login_window: bool = False):
    parts = urlsplit(origin)
    if parts.scheme != "https" or not parts.hostname or parts.port not in (None, 443):
        raise PanelError("origin_rejected")
    profile = validate_profile(profile_dir, account_id)
    allowed = _allowed(parts.hostname, allowed_hosts)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise PanelError("browser_missing") from None
    with exclusive_profile(str(profile)):
        try:
            playwright_cm = sync_playwright()
        except Exception:
            raise PanelError("browser_missing") from None
        with playwright_cm as playwright:
            context = None
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), channel="chrome", headless=headless,
                    chromium_sandbox=True, timeout=20_000,
                    accept_downloads=False, service_workers="block",
                    args=["--disable-background-networking", "--disable-component-update"],
                )
                if login_window:
                    context.route("**/*", _route(allowed))
                    context.route_web_socket("**/*", lambda socket: socket.close())
                    yield LoginWindow(context, origin)
                else:
                    yield RequestPanel(context, origin)
            except PanelError:
                raise
            except Exception:
                raise PanelError("browser_unavailable") from None
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        raise PanelError("browser_unavailable") from None
