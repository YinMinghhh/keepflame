"""A redirect-safe HTTP capability based on the Python standard library."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 1_048_576


class HttpTransportError(RuntimeError):
    pass


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
        except (TimeoutError, socket.timeout, URLError) as exc:
            raise HttpTransportError("HTTP request failed before a response was received") from exc

