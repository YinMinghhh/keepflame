"""Native transport and request budget checks; no browser or external I/O."""

import json
import time
from types import SimpleNamespace

import pytest

from keepflame.flames.joinquant.guard import FlowError, SIGNIN_PATH, SignInGuard
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN, TASK_INFO_URL


class Reply:
    def __init__(self, status=200, body=b'{"code":"00000"}', headers=None):
        self.status, self.data, self.headers = status, body, headers or {}
        self.disposed = False
    def body(self):
        return self.data
    def dispose(self):
        self.disposed = True


def setup(reply):
    calls = []
    def get(url, **options):
        calls.append((url, options))
        return reply
    page = SimpleNamespace(context=SimpleNamespace(route=lambda *_:None, request=SimpleNamespace(get=get)))
    return SignInGuard(page, deadline=time.monotonic()+60), calls


def test_native_status_uses_fixed_target_no_redirects_no_retries_or_page_fetch():
    response = Reply(body=b'{"code":"00000","data":{"taskList":{"login":{"status":0}}}}')
    guard, calls = setup(response)
    result = guard.read_status()
    assert result.body == response.data and response.disposed
    assert calls[0][0] == TASK_INFO_URL
    assert calls[0][1]["max_redirects"] == calls[0][1]["max_retries"] == 0
    assert 0 < calls[0][1]["timeout"] <= 10_000
    assert "cookie" not in {key.lower() for key in calls[0][1]["headers"]}
    for _ in range(3):
        guard.read_status()
    with pytest.raises(FlowError,match="status_budget"):
        guard.read_status()
    assert len(calls) == 4


@pytest.mark.parametrize("status", [302, 403, 429])
def test_native_status_errors_preserve_status_and_retry_after(status):
    guard, calls = setup(Reply(status=status, headers={"retry-after":"172800"}))
    with pytest.raises(FlowError) as failure:
        guard.read_status()
    assert failure.value.status == status and failure.value.retry_after == "172800"
    assert len(calls) == 1


class Route:
    def __init__(self, response, *, url=JOINQUANT_ORIGIN+SIGNIN_PATH, method="POST", body=None):
        self.request = SimpleNamespace(url=url, method=method, resource_type="fetch",
            headers={"content-type":"application/x-www-form-urlencoded"},
            post_data=body or "ruleKey=login&valideCode=synthetic-token&appKey=synthetic-key&appSecret=synthetic-secret")
        self.response, self.fetched, self.delivered, self.aborted = response, [], [], []
    def fetch(self, **options):
        self.fetched.append(options)
        return self.response
    def fulfill(self, **options):
        self.delivered.append(options)
    def abort(self, reason):
        self.aborted.append(reason)
    def fallback(self):
        raise AssertionError("no uncontrolled forwarding of writes")


def test_native_signin_does_not_follow_redirect_or_deliver_it_to_page():
    guard, _ = setup(Reply())
    guard.phase = "submit"
    reply = Reply(status=307, headers={"location":"/synthetic/unrelated"})
    route = Route(reply)
    guard.route(route)
    assert len(route.fetched) == 1 and route.fetched[0]["max_redirects"] == route.fetched[0]["max_retries"] == 0
    assert route.aborted and not route.delivered and reply.disposed
    assert guard.failure.status == 307 and guard.failure.reason == "signin_http"
    guard.route(Route(Reply()))
    assert guard.counts[SIGNIN_PATH] == 1


@pytest.mark.parametrize("url,method,body", [
    (JOINQUANT_ORIGIN+"/synthetic/unrelated","POST",None),
    ("https://cdn.joinquant.com/asset","POST",None),
    (JOINQUANT_ORIGIN+SIGNIN_PATH,"DELETE",None),
    (JOINQUANT_ORIGIN+SIGNIN_PATH+"?ruleKey=other","POST",None),
    (JOINQUANT_ORIGIN+SIGNIN_PATH,"POST","ruleKey=other&valideCode=x&appKey=x&appSecret=x"),
])
def test_unknown_write_scope_never_reaches_native_transport(url,method,body):
    guard, _ = setup(Reply())
    guard.phase = "submit"
    route = Route(Reply(),url=url,method=method,body=body)
    guard.route(route)
    assert route.aborted and not route.fetched and not route.delivered
    assert guard.failure is not None
