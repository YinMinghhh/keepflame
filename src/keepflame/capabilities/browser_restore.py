"""Opt-in native session restore with a barrier present before Chrome starts.

The reserved TCP port never listens or forwards. Chrome uses it as its only
proxy, including for loopback URLs. Keep it reserved until Chrome has closed.
Playwright's Node-side API transport remains subject to the existing request
guards; do not configure a Playwright proxy or switch the encryption provider.
"""

from contextlib import contextmanager
from importlib.metadata import version
import inspect
import socket
import time


def require_bounded_driver():
    # These timeout metadata semantics and driver EOF cleanup were verified in
    # this version. An upgrade must be reviewed before enabling native restore.
    if version("playwright") != "1.63.0":
        raise RuntimeError("native restore requires reviewed Playwright 1.63.0")


@contextmanager
def restore_deadline(playwright, seconds):
    """Supply an actual driver deadline for APIs with no public timeout option.

    route()/unroute()/new_page()/close() otherwise send timeout=None; the default
    page timeout does not apply. Scope this private adapter to one owned driver,
    never global Playwright state. Keep the original sync API/handler mapping.
    """
    connection = playwright._impl_obj._connection
    original = connection._send_message_to_server
    if tuple(inspect.signature(original).parameters) != ("object", "method", "params", "timeout", "no_reply"):
        raise RuntimeError("unsupported browser deadline transport")
    deadline = time.monotonic() + seconds

    def send(object, method, params, timeout, no_reply=False):
        remaining = (deadline - time.monotonic()) * 1000
        if remaining <= 0:
            raise TimeoutError("browser restore deadline exceeded")
        limit = min(timeout, remaining) if timeout else remaining
        return original(object, method, params, limit, no_reply)

    connection._send_message_to_server = send
    try:
        yield
    finally:
        connection._send_message_to_server = original


@contextmanager
def restore_barrier():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocked:
        blocked.bind(("127.0.0.1", 0))
        port = blocked.getsockname()[1]
        yield [
            "--restore-last-session",
            f"--proxy-server=http://127.0.0.1:{port}",
            "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND",
            "--disable-quic",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
