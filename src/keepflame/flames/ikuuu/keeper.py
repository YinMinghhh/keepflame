"""ikuuu (SSPanel-UIM / Malio) daily check-in with an imported session cookie."""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from keepflame.capabilities.http import HttpClient, HttpRequest, HttpResponse, HttpTransportError
from keepflame.capabilities.secrets import SecretReadError
from keepflame.core.models import HumanAction, JsonValue, KeepResult, KeepStatus
from keepflame.core.protocols import KeeperContext
from keepflame.flames.ikuuu.cookies import CookieError, SessionCookie, load_file
from keepflame.flames.ikuuu.discovery import (
    BROWSER_UA, DEFAULT_HOST_PATTERN, DEFAULT_NAV_SEEDS, LOGIN_PATH, DomainFinder, DomainSource,
    looks_like_login, normalize_base,
)

CHECKIN_PATH = "/user/checkin"
ACTION_KIND = "IKUUU_LOGIN_REQUIRED"
HUMAN_REASONS = {"session_expired", "domain_changed", "cookie_unreadable", "cookie_invalid", "cookie_unbound"}
_ALREADY = re.compile(r"已经签到|已签到|签到过|already", re.I)
_LOGIN_HINT = re.compile(r"未登录|请登录|登录失效|not logged|login", re.I)
_TRAFFIC = re.compile(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)", re.I)
_UNITS_MB = {"KB": 1 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1024.0 * 1024.0}
_SUMMARIES = {
    "session_expired": "ikuuu 登录会话已失效",
    "domain_changed": "ikuuu 登录域名已更换，需要在新域名重新登录",
    "cookie_unreadable": "ikuuu Cookie 文件不存在或权限不是 600",
    "cookie_invalid": "ikuuu Cookie 文件格式无效",
    "cookie_unbound": "ikuuu Cookie 未绑定登录域名",
}


class _Settings:
    def __init__(self, settings) -> None:
        def text(key, default=None):
            value = settings.get(key, default)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(key)
            return value

        cookie_file = text("cookie_file")
        if cookie_file is None or not cookie_file.startswith("/"):
            raise ValueError("cookie_file")
        self.cookie_file = cookie_file
        self.host_pattern = text("host_pattern", DEFAULT_HOST_PATTERN)
        try:
            re.compile(self.host_pattern)
        except re.error:
            raise ValueError("host_pattern") from None
        seeds = settings.get("nav_seeds", list(DEFAULT_NAV_SEEDS))
        if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 10
                or not all(isinstance(seed, str) and seed.startswith("https://") for seed in seeds)):
            raise ValueError("nav_seeds")
        self.seeds = tuple(seeds)
        base = text("base_url")
        self.base_url = normalize_base(base, self.host_pattern) if base else None
        if base and self.base_url is None:
            raise ValueError("base_url")
        self.max_probes = _int(settings, "max_probes", 8, 1, 20)
        self.timeout = float(_int(settings, "timeout_seconds", 15, 3, 60))
        pause = settings.get("request_pause_seconds", 1.0)
        if isinstance(pause, bool) or not isinstance(pause, (int, float)) or not 0 <= pause <= 10:
            raise ValueError("request_pause_seconds")
        self.pause = float(pause)


def _int(settings, key, default, low, high):
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(key)
    return value


def _clean_message(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]*>", "", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or None


def traffic_from_message(message: str | None) -> tuple[str | None, float | None]:
    match = _TRAFFIC.search(message or "")
    if not match:
        return None, None
    amount, unit = float(match.group(1)), match.group(2).upper()
    return f"{match.group(1)}{unit}", round(amount * _UNITS_MB[unit], 3)


def _retry_after(response: HttpResponse, now: datetime) -> datetime | None:
    value = response.header("retry-after")
    if response.status not in {429, 502, 503, 504} or not value or len(value) > 128:
        return None
    try:
        if value.strip().isdigit():
            candidate = now + timedelta(seconds=min(int(value), 7 * 86400))
        else:
            candidate = parsedate_to_datetime(value)
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=timezone.utc)
        return candidate.astimezone(timezone.utc) if candidate > now else None
    except (ValueError, TypeError, OverflowError):
        return None


class IkuuuKeeper:
    business_timezone = "Asia/Shanghai"

    def __init__(self, *, sleeper: Callable[[float], None] = time.sleep,
                 extra_sources: Iterable[DomainSource] = ()) -> None:
        self.sleeper = sleeper
        self.extra_sources = tuple(extra_sources)

    # -- results --------------------------------------------------------------------
    @staticmethod
    def _login_action(flame_id: str, base: str | None, reason: str, config_path: str | None = None) -> HumanAction:
        login_url = f"{base}{LOGIN_PATH}" if base else None
        base_arg = f" --base {base}" if base else " --base https://<当前域名>"
        config_arg = f" --config {shlex.quote(config_path)}" if config_path else ""
        return HumanAction(
            kind=ACTION_KIND,
            summary=_SUMMARIES.get(reason, "ikuuu 需要重新登录"),
            instructions=(f"在浏览器打开登录页完成登录（含验证码），复制 Cookie 后在 KeepFlame 主机执行："
                          f"pbpaste | keepflame ikuuu import-cookie{config_arg} --flame {shlex.quote(flame_id)}{base_arg}"),
            url=login_url,
        )

    def _human(self, context, base, reason, metadata) -> KeepResult:
        return KeepResult(
            KeepStatus.WAITING_HUMAN, f"ikuuu check-in needs a fresh login: {reason}",
            metadata={**metadata, "reason": reason, "base_url": base},
            human_action=self._login_action(context.flame_id, base, reason, context.config_path),
        )

    @staticmethod
    def _failed(reason, metadata, *, paused=False, next_action_at=None, message=None) -> KeepResult:
        data = {**metadata, "reason": reason}
        if paused:
            data["automation_paused"] = True
        return KeepResult(KeepStatus.FAILED, message or f"ikuuu check-in failed: {reason}",
                          metadata=data, next_action_at=next_action_at)

    # -- run ------------------------------------------------------------------------
    def keep(self, context: KeeperContext) -> KeepResult:
        metadata: dict[str, JsonValue] = {"keeper": "ikuuu"}
        try:
            settings = _Settings(context.settings)
        except ValueError as exc:
            return self._failed("configuration_invalid", {**metadata, "setting": str(exc)}, paused=True,
                                message="ikuuu settings are invalid")
        http = context.capabilities.require("http", HttpClient)
        previous = context.storage.get_state(context.flame_id, context.account_id)
        previous_meta = previous.metadata if previous is not None else {}
        previous_base = previous_meta.get("base_url") if isinstance(previous_meta.get("base_url"), str) else None

        cookie: SessionCookie | None = None
        cookie_problem = None
        try:
            cookie = load_file(context.secrets.read_text(settings.cookie_file), host_pattern=settings.host_pattern)
        except SecretReadError:
            cookie_problem = "cookie_unreadable"
        except CookieError:
            cookie_problem = "cookie_invalid"
        fingerprint = cookie.fingerprint if cookie is not None else cookie_problem
        metadata["import_tag"] = fingerprint

        # Still waiting on the same cookie today: no new requests, same action.
        if (previous is not None and previous.status is KeepStatus.WAITING_HUMAN
                and previous_meta.get("reason") in HUMAN_REASONS
                and previous_meta.get("import_tag") == fingerprint
                and context.business_target is not None
                and previous.last_checked_target == context.business_target):
            return self._human(context, previous_base, str(previous_meta["reason"]),
                               {**metadata, "network": "skipped_unchanged_import"})

        bound = None
        if cookie is not None:
            bound = cookie.base_url or settings.base_url
            if bound is None:
                cookie_problem = "cookie_unbound"
        finder = DomainFinder(http, seeds=settings.seeds, host_pattern=settings.host_pattern,
                              max_probes=settings.max_probes, timeout_seconds=settings.timeout,
                              pause_seconds=settings.pause, sleeper=self.sleeper,
                              extra_sources=self.extra_sources)
        preferred = [value for value in (bound, settings.base_url, previous_base) if value]
        found = finder.discover(preferred)
        metadata.update({
            "discovery": list(found.methods), "candidates": len(found.candidates),
            "probed": len(found.probed), "seeds_reached": found.seeds_reached,
        })
        base = found.base_url
        if base is None:
            # Nothing reachable: bounded same-day retries, then one retries_exhausted alert.
            return self._failed("domain_discovery_failed", {**metadata, "base_url": previous_base})
        if cookie_problem is not None:
            return self._human(context, base, cookie_problem, metadata)
        if bound != base:
            # Never send the session cookie to a host it was not imported for.
            return self._human(context, base, "domain_changed", {**metadata, "imported_for_base_url": bound})
        return self._checkin(context, http, settings, cookie, base, metadata)

    def _checkin(self, context, http, settings, cookie, base, metadata) -> KeepResult:
        now = context.clock.now() if context.clock is not None else context.now
        request = HttpRequest("POST", base + CHECKIN_PATH, headers={
            "User-Agent": BROWSER_UA, "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest", "Origin": base, "Referer": base + "/user",
            "Cookie": cookie.header,
        }, body=b"")
        try:
            response = http.send(request, timeout_seconds=settings.timeout)
        except HttpTransportError:
            return self._failed("checkin_transport_error", metadata)
        metadata = {**metadata, "remote_status": response.status}
        if response.status in {301, 302, 303, 307, 308}:
            location = response.header("location") or ""
            target = urlsplit(urljoin(base + CHECKIN_PATH, location))
            if target.path.startswith("/auth/"):
                return self._human(context, base, "session_expired", metadata)
            return self._failed("checkin_unexpected_redirect", metadata)
        if response.status in {401, 403}:
            return self._human(context, base, "session_expired", metadata)
        if response.status != 200:
            return self._failed("checkin_http_error", metadata, next_action_at=_retry_after(response, now))
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, ValueError):
            if looks_like_login(response.body):
                return self._human(context, base, "session_expired", metadata)
            return self._failed("checkin_not_json", metadata)
        if not isinstance(payload, dict):
            return self._failed("checkin_not_json", metadata)
        ret = payload.get("ret")
        message = _clean_message(payload.get("msg"))
        metadata = {**metadata, "ret": ret if type(ret) is int else None, "remote_message": message}
        if ret == 1:
            traffic, megabytes = traffic_from_message(message)
            return KeepResult(KeepStatus.SUCCESS, message or "ikuuu check-in succeeded", metadata={
                **metadata, "checkin_state": "checked_in", "traffic_gained": traffic,
                "traffic_gained_mb": megabytes, "base_url": base, "checked_in_at": now.astimezone(timezone.utc).isoformat(),
            })
        if message and _ALREADY.search(message):
            return KeepResult(KeepStatus.SUCCESS, message, metadata={
                **metadata, "checkin_state": "already_checked_in", "base_url": base,
                "checked_in_at": now.astimezone(timezone.utc).isoformat(),
            })
        if message and _LOGIN_HINT.search(message):
            return self._human(context, base, "session_expired", metadata)
        return self._failed("checkin_rejected", {**metadata, "base_url": base})
