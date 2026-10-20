"""A redirect-safe HTTP capability based on the Python standard library."""

from __future__ import annotations

import http.client
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 1_048_576


class HttpTransportError(RuntimeError):
    """No HTTP response was received (connect, TLS, timeout, reset, truncated read).

    ``error_type`` names the underlying exception class (e.g. ``SSLEOFError``) for
    diagnostics; it never carries URLs, headers or bodies.
    """

    def __init__(self, message: str = "HTTP request failed before a response was received", *,
                 error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type or "HttpTransportError"


def transport_error_type(exc: BaseException) -> str:
    """Innermost useful exception class name: urllib wraps TLS/socket errors in URLError."""
    if isinstance(exc, HttpTransportError):
        return exc.error_type
    reason = getattr(exc, "reason", None)
    if isinstance(exc, URLError) and isinstance(reason, BaseException):
        return type(reason).__name__
    return type(exc).__name__


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes = field(repr=False)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.casefold())


@runtime_checkable
class HttpClient(Protocol):
    def send(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_limited(handle) -> bytes:
    data = handle.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        return data[:MAX_RESPONSE_BYTES]
    return data


class UrllibHttpClient:
    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def send(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse:
        raw_request = Request(
            request.url,
            data=request.body,
            headers=request.headers,
            method=request.method,
        )
        try:
            with self._opener.open(raw_request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=response.status,
                    url=response.geturl(),
                    headers={key.casefold(): value for key, value in response.headers.items()},
                    body=_read_limited(response),
                )
        except HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                url=exc.geturl(),
                headers={key.casefold(): value for key, value in exc.headers.items()},
                body=_read_limited(exc),
            )
        # HTTPError (a URLError) is handled above. Everything else here means no usable
        # response: DNS/connect/TLS failures (incl. SSLEOFError), timeouts, resets, and
        # truncated responses raised while reading the body.
        except (URLError, OSError, http.client.HTTPException) as exc:
            raise HttpTransportError(error_type=transport_error_type(exc)) from exc

