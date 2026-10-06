"""Inspect the account-level JoinQuant daily sign-in state."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

from keepflame.capabilities.http import (
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
)
from keepflame.capabilities.secrets import SecretReadError
from keepflame.core.models import HumanAction, KeepResult, KeepStatus, JsonValue
from keepflame.core.protocols import KeeperContext

JOINQUANT_ORIGIN = "https://www.joinquant.com"
TASK_INFO_URL = f"{JOINQUANT_ORIGIN}/credits/task/getTaskInfo"
SIGNIN_PAGE_URL = f"{JOINQUANT_ORIGIN}/view/user/floor?type=creditsdesc"
LOGIN_PATH_PREFIX = "/user/login"
RETRYABLE_STATUSES = {502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
TIMEOUT_SECONDS = 15.0


class _DuplicateJSONMember(ValueError):
    pass


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            # Keys are already Unicode-decoded by json.loads. Never retain or
            # expose the duplicate name, values, or a partially selected object.
            raise _DuplicateJSONMember
        result[key] = value
    return result


def _metadata(code: str, task_status: int | None = None, server_time=None) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"remote_code": code}
    if task_status is not None:
        result["task_status"] = task_status
    if isinstance(server_time, (str, int, float)) and not isinstance(server_time, bool):
        result["server_time"] = server_time
    return result


def _status_shape(payload: dict, task: str = "checkin") -> dict[str, str]:
    """Record only fixed-path JSON types, never response values or arbitrary keys."""
    missing = object()
    parent = payload
    shape = {}
    kinds = {dict: "object", list: "array", str: "string", int: "integer", float: "number", bool: "boolean", type(None): "null"}
    rule = "post_view" if task == "reading" else "login"
    task_record = None
    for key in ("data", "taskList", rule, "status"):
        if not isinstance(parent, dict):
            shape[key] = "unavailable"
            parent = missing
            continue
        value = parent.get(key, missing)
        shape[key] = "missing" if value is missing else kinds.get(type(value), "unknown")
        if key == rule:
            task_record = value
        parent = value
    if task == "reading":
        count = task_record.get("totalCount", missing) if isinstance(task_record, dict) else missing
        shape["totalCount"] = ("unavailable" if not isinstance(task_record, dict) else
                               "missing" if count is missing else kinds.get(type(count), "unknown"))
    return shape


def _signin_action() -> HumanAction:
    return HumanAction(
        kind="COMPLETE_OFFICIAL_SIGNIN",
        summary="聚宽今日尚未签到",
        instructions="请在聚宽官方积分页面完成滑块验证和签到；KeepFlame 将在下一次复检时确认结果。",
        url=SIGNIN_PAGE_URL,
    )


def _refresh_session_action() -> HumanAction:
    return HumanAction(
        kind="REFRESH_SESSION",
        summary="聚宽登录会话需要更新",
        instructions="请从已登录的聚宽会话更新仓库外 Cookie 密钥文件，然后等待下一次复检。",
        url=SIGNIN_PAGE_URL,
    )


def _browser_challenge_action() -> HumanAction:
    return HumanAction(
        kind="COMPLETE_BROWSER_CHALLENGE",
        summary="聚宽要求浏览器人工验证",
        instructions="请在聚宽官方页面完成安全验证；KeepFlame 不会尝试绕过验证。",
        url=SIGNIN_PAGE_URL,
    )


class JoinQuantKeeper:
    browser_origin = JOINQUANT_ORIGIN
    login_url = JOINQUANT_ORIGIN + "/user/login"
    login_blocked_paths = ("/credits/", "/common/verifyCode/", "/user/account/getToken", "/community/post/postView")

    def __init__(self, sleeper: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleeper

    def keep(self, context: KeeperContext) -> KeepResult:
        task = context.settings.get("task", "checkin")
        if task not in ("checkin", "reading"):
            return KeepResult(KeepStatus.FAILED, "Unknown JoinQuant task", metadata={"error_type": "configuration"})
        mode = context.settings.get("mode", "inspect")
        if mode == "browser":
            from keepflame.flames.joinquant.browser import JoinQuantBrowserKeeper

            return JoinQuantBrowserKeeper().keep(context)
        if mode != "inspect":
            return KeepResult(KeepStatus.FAILED, "Unknown JoinQuant mode", metadata={"error_type": "configuration"})
        cookie_file = context.settings.get("cookie_file")
        if not isinstance(cookie_file, str) or not cookie_file.strip():
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant cookie_file must be a non-empty absolute path",
                metadata={"error_type": "configuration"},
            )

        try:
            raw_cookie = context.secrets.read_text(cookie_file)
        except SecretReadError:
            return KeepResult(
                status=KeepStatus.WAITING_HUMAN,
                message="JoinQuant session secret is unavailable",
                metadata={"error_type": "session_unavailable", "automation_paused": True, "pause_account": True},
                human_action=_refresh_session_action(),
            )

        cookie_error = self._validate_cookie(raw_cookie)
        if cookie_error is not None:
            return KeepResult(
                status=KeepStatus.WAITING_HUMAN,
                message=cookie_error,
                metadata={"error_type": "session_invalid", "automation_paused": True, "pause_account": True},
                human_action=_refresh_session_action(),
            )

        http = context.capabilities.require("http", HttpClient)
        request = HttpRequest(
            method="GET",
            url=TASK_INFO_URL,
            headers={
                "Accept": "application/json",
                "Cookie": raw_cookie,
                "User-Agent": "KeepFlame/0.1",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        response: HttpResponse | None = None
        for attempt in range(2):
            try:
                response = http.send(request, timeout_seconds=TIMEOUT_SECONDS)
            except HttpTransportError:
                if attempt == 0:
                    self._sleep(1.0)
                    continue
                return KeepResult(
                    status=KeepStatus.FAILED,
                    message="JoinQuant status request failed before receiving a response",
                    metadata={"error_type": "network"},
                )
            if response.status in RETRYABLE_STATUSES and attempt == 0 and self._retry_after(response, context.now) is None:
                self._sleep(1.0)
                continue
            break

        if response is None:
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant status request produced no response",
                metadata={"error_type": "network"},
            )
        return self._inspection_result(self._interpret(response, now=context.now, task=task))

    @staticmethod
    def _inspection_result(result):
        """Do not keep polling expired credentials or an unknown reward protocol."""
        status = result.metadata.get("remote_status")
        auth = (status in (401, 403) or result.human_action is not None
                and result.human_action.kind in {"REFRESH_SESSION", "COMPLETE_BROWSER_CHALLENGE"})
        protocol = (result.status is KeepStatus.FAILED and
                    (result.metadata.get("error_type") == "protocol" or "status_shape" in result.metadata
                     or status in REDIRECT_STATUSES | {200} or status is None))
        if auth or protocol or status == 429:
            return replace(result, metadata={**result.metadata, "automation_paused": True,
                                              **({"pause_account": True} if auth else {})})
        return result

    @staticmethod
    def _validate_cookie(raw_cookie: str) -> str | None:
        if not raw_cookie:
            return "JoinQuant session secret is empty"
        if "\r" in raw_cookie or "\n" in raw_cookie:
            return "JoinQuant session secret must be a single line without a Cookie prefix"
        if raw_cookie.casefold().startswith("cookie:"):
            return "JoinQuant session secret must contain only the raw Cookie header value"
        if "=" not in raw_cookie:
            return "JoinQuant session secret is not a valid Cookie header value"
        return None

    @classmethod
    def _interpret(cls, response: HttpResponse, *, now: datetime, task: str = "checkin") -> KeepResult:
        if task not in ("checkin", "reading"):
            return KeepResult(KeepStatus.FAILED, "Unknown JoinQuant task", metadata={"error_type": "configuration"})
        text = response.body.decode("utf-8", errors="replace")
        if response.status in REDIRECT_STATUSES:
            location = response.header("location") or ""
            if cls._is_login_location(location):
                return KeepResult(
                    status=KeepStatus.WAITING_HUMAN,
                    message="JoinQuant redirected the status request to login",
                    metadata={"remote_status": response.status},
                    human_action=_refresh_session_action(),
                )
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant returned an unexpected redirect",
                metadata={"remote_status": response.status},
            )
        if response.status == 401:
            return KeepResult(
                status=KeepStatus.WAITING_HUMAN,
                message="JoinQuant rejected the expired login session",
                metadata={"remote_status": response.status},
                human_action=_refresh_session_action(),
            )
        if response.status == 403 and cls._looks_like_challenge(text):
            return KeepResult(
                status=KeepStatus.WAITING_HUMAN,
                message="JoinQuant requires an interactive browser verification",
                metadata={"remote_status": response.status},
                human_action=_browser_challenge_action(),
            )
        if response.status != 200:
            return KeepResult(
                status=KeepStatus.FAILED,
                message=f"JoinQuant status request returned HTTP {response.status}",
                next_action_at=cls._retry_after(response, now),
                metadata={"remote_status": response.status},
            )

        try:
            payload = json.loads(text, object_pairs_hook=_unique_json_object)
        except _DuplicateJSONMember:
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant status response contains duplicate JSON members",
                metadata={"error_type": "protocol", "protocol_reason": "duplicate_json_member",
                          "reading_state" if task == "reading" else "signin_state": "unknown"},
            )
        except json.JSONDecodeError:
            if cls._looks_like_challenge(text):
                return KeepResult(
                    status=KeepStatus.WAITING_HUMAN,
                    message="JoinQuant returned an interactive login or verification page",
                    metadata={"remote_status": response.status},
                    human_action=_browser_challenge_action(),
                )
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant status response was not JSON",
                metadata={"remote_status": response.status},
            )
        if not isinstance(payload, dict):
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant status response had an unexpected shape",
                metadata={"error_type": "protocol"},
            )

        code = str(payload.get("code", ""))
        redirect = payload.get("redirect")
        if code == "10001" or (isinstance(redirect, str) and cls._is_login_location(redirect)):
            return KeepResult(
                status=KeepStatus.WAITING_HUMAN,
                message="JoinQuant login session has expired",
                metadata=_metadata(code or "10001"),
                human_action=_refresh_session_action(),
            )
        if code != "00000":
            if code == "20000":
                message = "JoinQuant rejected the request method or required headers"
            else:
                message = "JoinQuant returned an application error"
            return KeepResult(
                status=KeepStatus.FAILED,
                message=message,
                metadata=_metadata(code or "missing"),
            )

        if task == "reading":
            return cls._reading_result(payload)
        shape = _status_shape(payload)
        data = payload.get("data")
        if not isinstance(data, dict):
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant response was missing data",
                metadata={**_metadata(code), "status_shape": shape},
            )
        task_list = data.get("taskList")
        if not isinstance(task_list, dict):
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant response was missing a valid task list",
                metadata={**_metadata(code), "status_shape": shape},
            )
        login_task = task_list.get("login")
        # Official credit-center defaults login to {} and treats an undefined
        # status as eligible. Absence is not evidence of completed sign-in.
        absent = "login_record_absent" if "login" not in task_list else (
            "login_status_absent" if isinstance(login_task, dict) and "status" not in login_task else None
        )
        if absent is not None:
            return KeepResult(
                status=KeepStatus.WAITING_HUMAN,
                message="JoinQuant daily sign-in is eligible under the public task schema",
                metadata={**_metadata(code), "signin_state": "eligible", "eligibility_basis": absent, "status_shape": shape},
                human_action=_signin_action(),
            )
        status = login_task.get("status") if isinstance(login_task, dict) else None
        valid_status = (type(status) is int and status in (0, 1)) or (
            isinstance(status, str) and status in ("0", "1")
        )
        if not valid_status:
            return KeepResult(
                status=KeepStatus.FAILED,
                message="JoinQuant response was missing a valid login task status",
                metadata={**_metadata(code), "status_shape": shape},
            )

        normalized = int(status)
        metadata = _metadata(code, normalized, data.get("currTime"))
        if normalized == 1:
            return KeepResult(
                status=KeepStatus.SUCCESS,
                message="JoinQuant daily sign-in is complete",
                metadata=metadata,
            )
        return KeepResult(
            status=KeepStatus.WAITING_HUMAN,
            message="JoinQuant daily sign-in still requires human action",
            metadata=metadata,
            human_action=_signin_action(),
        )

    @staticmethod
    def _reading_result(payload) -> KeepResult:
        """Reading absence means qualification is needed, never claim eligibility."""
        shape = _status_shape(payload, "reading")
        metadata = {"remote_code": "00000", "status_shape": shape, "reading_state": "unknown"}

        def unknown():
            return KeepResult(KeepStatus.FAILED, "JoinQuant reading task has an unsupported status shape",
                              metadata={**metadata, "error_type": "protocol", "protocol_reason": "reading_status_shape"})

        data = payload.get("data")
        tasks = data.get("taskList") if isinstance(data, dict) else None
        if not isinstance(tasks, dict):
            return unknown()
        record = tasks.get("post_view", {})
        if not isinstance(record, dict):
            return unknown()
        if "totalCount" in record:
            count = record["totalCount"]
            valid = type(count) is int and count >= 0 or (
                type(count) is str and count.isascii() and count.isdigit()
                and len(count) <= 20 and (count == "0" or not count.startswith("0")))
            if not valid:
                return unknown()
            metadata["available_credits"] = int(count)
        if "status" not in record:
            return KeepResult(KeepStatus.WAITING_HUMAN, "JoinQuant reading qualification has not been observed",
                metadata={**metadata, "reading_state": "needs_reading"},
                human_action=HumanAction("READ_OFFICIAL_ARTICLE", "聚宽阅读任务尚未取得资格",
                    "在已授权的独立浏览器中正常阅读配置的公开文章；前端计时不是后端资格保证。", SIGNIN_PAGE_URL))
        status = record["status"]
        if not ((type(status) is int and status in (0, 1)) or (type(status) is str and status in ("0", "1"))):
            return unknown()
        metadata["task_status"] = int(status)
        if int(status) == 1:
            return KeepResult(KeepStatus.SUCCESS, "JoinQuant backend reports the reading task complete",
                              metadata={**metadata, "reading_state": "complete"})
        if "available_credits" not in metadata:
            return unknown()
        return KeepResult(KeepStatus.WAITING_HUMAN, "JoinQuant reading reward is available to claim",
            metadata={**metadata, "reading_state": "claimable"},
            human_action=HumanAction("CLAIM_OFFICIAL_READING_REWARD", "聚宽阅读积分待领取",
                "仅在官方阅读任务卡片领取；完成后由后端任务状态确认，不根据点击或提示判定成功。", SIGNIN_PAGE_URL))

    @staticmethod
    def _retry_after(response: HttpResponse, now: datetime) -> datetime | None:
        if response.status not in RETRYABLE_STATUSES | {429}:
            return None
        value = response.header("retry-after")
        if not value:
            return None
        try:
            if value.strip().isdigit():
                candidate = now + timedelta(seconds=int(value))
            else:
                candidate = parsedate_to_datetime(value)
                if candidate.tzinfo is None:
                    candidate = candidate.replace(tzinfo=timezone.utc)
            return candidate.astimezone(timezone.utc) if candidate > now else None
        except (ValueError, TypeError, OverflowError):
            return None

    @staticmethod
    def _is_login_location(location: str) -> bool:
        if not location:
            return False
        absolute = urljoin(JOINQUANT_ORIGIN, location)
        parsed = urlparse(absolute)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "www.joinquant.com"
            and parsed.path.startswith(LOGIN_PATH_PREFIX)
        )

    @staticmethod
    def _looks_like_challenge(text: str) -> bool:
        lowered = text.casefold()
        return any(
            marker in lowered
            for marker in ("captcha", "login", "安全验证", "滑块", "验证码", "登录")
        )
