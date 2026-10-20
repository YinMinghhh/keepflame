"""UrllibHttpClient folds every no-response failure into HttpTransportError with its cause type."""

import http.client
import socket
import ssl
from urllib.error import URLError

import pytest

from keepflame.capabilities.http import HttpRequest, HttpTransportError, UrllibHttpClient, transport_error_type


class Opener:
    def __init__(self, exc):
        self.exc = exc

    def open(self, request, timeout):
        raise self.exc


@pytest.mark.parametrize("exc, expected", [
    (URLError(ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2427)")), "SSLEOFError"),
    (ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"), "SSLEOFError"),
    (URLError(ConnectionRefusedError(61, "refused")), "ConnectionRefusedError"),
    (URLError("name resolution failed"), "URLError"),
    (socket.timeout("timed out"), "TimeoutError"),
    (ConnectionResetError(54, "reset"), "ConnectionResetError"),
    (http.client.RemoteDisconnected("closed"), "RemoteDisconnected"),
    (http.client.IncompleteRead(b"partial"), "IncompleteRead"),
])
def test_transport_failures_keep_only_the_exception_class(exc, expected):
    client = UrllibHttpClient()
    client._opener = Opener(exc)
    with pytest.raises(HttpTransportError) as error:
        client.send(HttpRequest("POST", "https://tushare.pro/wctapi/user_center/sign",
                                headers={"Cookie": "uid=SYNTHETIC-SECRET"}, body=b""), timeout_seconds=3)
    assert error.value.error_type == expected
    assert "SYNTHETIC" not in str(error.value) and "tushare.pro" not in str(error.value)


def test_error_type_helper_and_default():
    assert HttpTransportError().error_type == "HttpTransportError"
    assert transport_error_type(HttpTransportError(error_type="SSLEOFError")) == "SSLEOFError"
    assert transport_error_type(URLError(ssl.SSLEOFError())) == "SSLEOFError"
