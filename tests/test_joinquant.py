import json
from datetime import datetime, timezone

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.capabilities.http import HttpTransportError
from keepflame.capabilities.secrets import SecretReadError
from keepflame.core.models import KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.joinquant.keeper import JoinQuantKeeper, TASK_INFO_URL
from tests.helpers import FakeHttpClient, MemoryStorage, StaticSecrets, response

COOKIE = "sid=test-session; other=value"


def test_login_window_allows_official_captcha_and_blocks_reward_submission():
    blocked = JoinQuantKeeper.login_blocked_paths

    def blocked_for(path: str) -> bool:
        return any(path.startswith(prefix) for prefix in blocked)

    assert not blocked_for("/user/login/doLoginByText")
    assert not blocked_for("/user/login/doLoginByCode")
    assert not blocked_for("/common/verifyCode/captchar")
    assert not blocked_for("/common/verifyCode/validate")
    assert not blocked_for("/common/verifyCode/valideCode")
    assert not blocked_for("/common/verifyCode/sendSMS")
    assert blocked_for("/credits/task/receiveCredits")
    assert blocked_for("/user/account/getToken")
    assert blocked_for("/community/post/postView")


def run_keeper(http: FakeHttpClient, secret=COOKIE):
    keeper = JoinQuantKeeper(sleeper=lambda _seconds: None)
    context = KeeperContext(
        flame_id="joinquant-primary",
        account_id="primary",
        settings={"cookie_file": "/run/secrets/joinquant.cookie"},
        capabilities=CapabilityRegistry({"http": http}),
        secrets=StaticSecrets(secret),
        storage=MemoryStorage(),
        now=datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc),
    )
    return keeper.keep(context)


@pytest.mark.parametrize("remote_status", [1, "1"])
def test_signed_in_maps_to_success(remote_status) -> None:
    payload = {"code": "00000", "data": {"currTime": "2026-08-11", "taskList": {"login": {"status": remote_status}}}}
    http = FakeHttpClient(response(200, json.dumps(payload)))
    result = run_keeper(http)
    assert result.status is KeepStatus.SUCCESS
    assert result.reward is None
    assert result.metadata == {
        "remote_code": "00000",
        "task_status": 1,
        "server_time": "2026-08-11",
    }
    request, timeout = http.requests[0]
    assert request.url == TASK_INFO_URL
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert request.headers["Cookie"] == COOKIE
    assert timeout == 15.0


@pytest.mark.parametrize("remote_status", [0, "0"])
def test_unsigned_maps_to_human_action(remote_status) -> None:
    payload = {"code": "00000", "data": {"taskList": {"login": {"status": remote_status}}}}
    result = run_keeper(FakeHttpClient(response(200, json.dumps(payload))))
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == "COMPLETE_OFFICIAL_SIGNIN"
    assert result.human_action.url.endswith("type=creditsdesc")


def test_expired_session_code_maps_to_refresh_action() -> None:
    result = run_keeper(
        FakeHttpClient(response(200, '{"redirect":"/user/login/index","status":"1","code":"10001","msg":""}'))
    )
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == "REFRESH_SESSION"


def test_login_redirect_maps_to_refresh_action() -> None:
    result = run_keeper(
        FakeHttpClient(
            response(302, "", headers={"Location": "/user/login/index"})
        )
    )
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == "REFRESH_SESSION"


def test_cross_origin_redirect_is_a_failure() -> None:
    result = run_keeper(
        FakeHttpClient(response(302, "", headers={"Location": "https://example.com/login"}))
    )
    assert result.status is KeepStatus.FAILED


def test_illegal_request_code_is_a_protocol_failure() -> None:
    result = run_keeper(
        FakeHttpClient(response(200, '{"code":"20000","msg":"不合法的请求方式"}'))
    )
    assert result.status is KeepStatus.FAILED
    assert result.metadata["remote_code"] == "20000"


def test_missing_login_record_is_eligible_not_complete() -> None:
    result = run_keeper(
        FakeHttpClient(response(200, '{"code":"00000","data":{"taskList":{}}}'))
    )
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == "COMPLETE_OFFICIAL_SIGNIN"
    assert result.metadata["eligibility_basis"] == "login_record_absent"
    assert "task_status" not in result.metadata


def test_html_challenge_is_waiting_human() -> None:
    result = run_keeper(
        FakeHttpClient(response(200, "<html>请完成滑块验证码</html>"))
    )
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == "COMPLETE_BROWSER_CHALLENGE"


def test_network_and_safe_gateway_failures_retry_once() -> None:
    success = response(
        200,
        '{"code":"00000","data":{"taskList":{"login":{"status":1}}}}',
    )
    network = FakeHttpClient(HttpTransportError("offline"), success)
    assert run_keeper(network).status is KeepStatus.SUCCESS
    assert len(network.requests) == 2

    gateway = FakeHttpClient(response(503, "unavailable"), success)
    assert run_keeper(gateway).status is KeepStatus.SUCCESS
    assert len(gateway.requests) == 2


def test_rate_limit_does_not_retry() -> None:
    http = FakeHttpClient(response(429, "slow down"))
    result = run_keeper(http)
    assert result.status is KeepStatus.FAILED
    assert len(http.requests) == 1


@pytest.mark.parametrize("secret", ["", "Cookie: sid=x", "sid=x\n", "not-a-cookie"])
def test_invalid_cookie_never_reaches_http(secret: str) -> None:
    http = FakeHttpClient()
    result = run_keeper(http, secret)
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == "REFRESH_SESSION"
    assert not http.requests
    if secret:
        assert secret not in json.dumps(result.metadata)
        assert secret not in result.message


def test_unreadable_cookie_is_waiting_human() -> None:
    result = run_keeper(FakeHttpClient(), SecretReadError("missing"))
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == "REFRESH_SESSION"
