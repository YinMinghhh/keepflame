"""Tushare daily points with an imported session cookie.

One Keeper class serves three Flames selected by ``settings.task``:

- ``sign``  : POST /wctapi/user_center/sign (DAILY_SIGN, +1)
- ``view``  : GET /document/1?doc_id=9, then POST /wctapi/user_center/view (VIEW_NEW_DATA, +2)
- ``guess`` : GET /wctapi/user_center/guess/current, vote with the majority, random side on a tie
  (+1, +4 if right)

Every request goes to https://tushare.pro only. ``sign`` and ``view`` first read
GET /wctapi/user_center/tasks and stop when the task is already completed, so a
retry after an uncertain outcome never repeats a credited action. ``guess`` is
idempotent through ``user_guess.vote``. ``code == 40201`` means the session is
gone and becomes WAITING_HUMAN; KeepFlame never logs in by itself.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from keepflame.capabilities.http import (
    HttpClient, HttpRequest, HttpResponse, HttpTransportError, transport_error_type,
)
from keepflame.capabilities.secrets import SecretReadError
from keepflame.core.models import FAILURE_CLASS_TRANSPORT, HumanAction, JsonValue, KeepResult, KeepStatus, Reward, RewardSource
from keepflame.core.protocols import KeeperContext
from keepflame.flames.tushare.cookies import ORIGIN, CookieError, SessionCookie, load_file

API_BASE = ORIGIN + "/wctapi"
TASKS_URL = API_BASE + "/user_center/tasks"
SIGN_URL = API_BASE + "/user_center/sign"
VIEW_URL = API_BASE + "/user_center/view"
GUESS_CURRENT_URL = API_BASE + "/user_center/guess/current"
GUESS_VOTE_URL = API_BASE + "/user_center/guess/vote"
NEW_DATA_PAGE_URL = ORIGIN + "/document/1?doc_id=9"
REFERER = ORIGIN + "/weborder/"
LOGIN_URL = ORIGIN + "/weborder/#/login"
# Slack only links bare https origin+path URLs (no fragment), so the clickable Action-URL is the
# web app root; it redirects to #/login when the session is gone. The full login link is in the text.
ACTION_URL = ORIGIN + "/weborder/"
SESSION_GONE_CODE = 40201
ACTION_KIND = "TUSHARE_LOGIN_REQUIRED"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
TASKS = ("sign", "view", "guess")
DEFAULT_TASK_CODES = {"sign": "DAILY_SIGN", "view": "VIEW_NEW_DATA"}
POINTS = {"sign": "1", "view": "2", "guess": "1"}
VOTE_UP, VOTE_DOWN = 1, 2
TIE_POLICIES = ("random", "up", "down")
LOG = logging.getLogger(__name__)
HUMAN_REASONS = {"session_expired", "cookie_unreadable", "cookie_invalid"}
# Confirmed in a real run: a successful sign and view both answer 「任务完成，积分已发放」.
# TODO(T0): confirm the repeated sign/view wording. "已达上限"/"已完成" come from the public
# shyz2006/tushare-checkin script; the others are tolerant guesses.
_ALREADY = re.compile(r"已达上限|已完成|已经?签到|已签|已领取|已浏览|重复|already", re.I)
_SUMMARIES = {
    "session_expired": "Tushare 登录会话已失效",
    "cookie_unreadable": "Tushare Cookie 文件不存在或权限不是 600",
    "cookie_invalid": "Tushare Cookie 文件格式无效",
}
_LABELS = {"sign": "sign-in", "view": "browse-new-data", "guess": "guess"}


class _Settings:
    def __init__(self, settings) -> None:
        cookie_file = settings.get("cookie_file")
        if not isinstance(cookie_file, str) or not cookie_file.startswith("/"):
            raise ValueError("cookie_file")
        self.cookie_file = cookie_file
        task = settings.get("task")
        if task not in TASKS:
            raise ValueError("task")
        self.task: str = task
        self.task_code = None
        if task in DEFAULT_TASK_CODES:
            code = settings.get("task_code", DEFAULT_TASK_CODES[task])
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
                raise ValueError("task_code")
            self.task_code = code
        elif "task_code" in settings:
            raise ValueError("task_code")
        tie = settings.get("guess_tie", "random")
        if tie not in TIE_POLICIES or (task != "guess" and "guess_tie" in settings):
            raise ValueError("guess_tie")
        self.guess_tie: str = tie
        verify = settings.get("verify_after_action", True)
        if not isinstance(verify, bool):
            raise ValueError("verify_after_action")
        self.verify = verify
        self.timeout = float(_int(settings, "timeout_seconds", 15, 3, 60))
        pause = settings.get("request_pause_seconds", 1.0)
        if isinstance(pause, bool) or not isinstance(pause, (int, float)) or not 0 <= pause <= 10:
            raise ValueError("request_pause_seconds")
        self.pause = float(pause)
        # Same-run retries for transport failures only (no HTTP response at all). Every new
        # TCP connect may land on another tushare.pro address, so a short constant pause works.
        self.transport_retries = _int(settings, "transport_retries", 4, 0, 5)
        backoff = settings.get("transport_backoff_seconds", 1.5)
        if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or not 0 <= backoff <= 10:
            raise ValueError("transport_backoff_seconds")
        self.transport_backoff = float(backoff)


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


def _count(value) -> int | None:
    """Vote counts may arrive as numbers or numeric strings (the web UI uses Number())."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def majority_vote(up_votes, down_votes, tie: str = "random",
                  choose: Callable[[Sequence[int]], int] | None = None) -> tuple[int, bool]:
    """Follow the majority; on an even split always vote anyway. Returns ``(vote, tied)``.

    A tie uses ``tie``: ``"random"`` picks up or down with ``choose`` (injectable for tests),
    ``"up"``/``"down"`` pin a side. The guess is never skipped.
    """
    up, down = _count(up_votes), _count(down_votes)
    if up is None or down is None:
        raise ValueError("vote counts are missing or not numeric")
    if up > down:
        return VOTE_UP, False
    if down > up:
        return VOTE_DOWN, False
    if tie == "up":
        return VOTE_UP, True
    if tie == "down":
        return VOTE_DOWN, True
    side = (choose or random.SystemRandom().choice)((VOTE_UP, VOTE_DOWN))
    if side not in (VOTE_UP, VOTE_DOWN):
        raise ValueError("tie chooser must return 1 or 2")
    return side, True


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


class _PostTransport(Exception):
    """A POST got no response; it may or may not have reached the server."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class _Stop(Exception):
    """Carries a finished KeepResult out of a nested request helper."""

    def __init__(self, result: KeepResult) -> None:
        super().__init__(result.message)
        self.result = result


class TushareKeeper:
    business_timezone = "Asia/Shanghai"

    def __init__(self, *, sleeper: Callable[[float], None] = time.sleep,
                 choose: Callable[[Sequence[int]], int] | None = None) -> None:
        self.sleeper = sleeper
        # Picks the side on a tied guess; tests inject a seeded or fixed chooser.
        self.choose = choose or random.SystemRandom().choice

    # -- results --------------------------------------------------------------------
    @staticmethod
    def _login_action(flame_id: str, reason: str) -> HumanAction:
        return HumanAction(
            kind=ACTION_KIND,
            summary=_SUMMARIES.get(reason, "Tushare 需要重新登录"),
            instructions=(f"在浏览器打开 {LOGIN_URL} 完成登录（含验证码），复制 tushare.pro 的 Cookie 后在 KeepFlame 主机执行："
                          f"pbpaste | keepflame tushare import-cookie --flame {flame_id}"
                          "（三个 Tushare Flame 共用同一个 Cookie 文件，导入一次即可）"),
            url=ACTION_URL,
        )

    def _human(self, context, task, reason, metadata) -> KeepResult:
        return KeepResult(
            KeepStatus.WAITING_HUMAN, f"Tushare {_LABELS[task]} needs a fresh login: {reason}",
            metadata={**metadata, "reason": reason},
            human_action=self._login_action(context.flame_id, reason),
        )

    @staticmethod
    def _failed(task, reason, metadata, *, paused=False, next_action_at=None, message=None) -> KeepResult:
        data = {**metadata, "reason": reason}
        if paused:
            data["automation_paused"] = True
        return KeepResult(KeepStatus.FAILED, message or f"Tushare {_LABELS.get(task, task)} failed: {reason}",
                          metadata=data, next_action_at=next_action_at)

    @staticmethod
    def _success(task, message, metadata, now, *, claimed: bool) -> KeepResult:
        reward = Reward(POINTS[task], "积分", RewardSource.CLAIMED) if claimed else None
        return KeepResult(KeepStatus.SUCCESS, message, reward=reward, metadata={
            **metadata, "completed_at": now.astimezone(timezone.utc).isoformat(),
        })

    # -- transport ------------------------------------------------------------------
    def _send(self, context, http, settings, cookie, task, method, url, step, metadata, now, *, page=False,
              body: bytes | None = None) -> HttpResponse:
        if not url.startswith(ORIGIN + "/"):
            raise AssertionError("Tushare requests stay on https://tushare.pro")  # never send the cookie elsewhere
        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8" if page else "application/json, text/plain, */*",
            "Referer": REFERER, "Cookie": cookie.header,
        }
        if not page:
            # Browsers send the bare origin; the web app lives under /weborder/ (Referer).
            headers["Origin"] = ORIGIN
        if body is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        # GETs are read-only and retried here. POSTs are never resent blindly: the caller
        # re-reads remote state first (_post_once_safely).
        attempts = 1 + settings.transport_retries if method == "GET" else 1
        attempt = 0
        while True:
            attempt += 1
            try:
                response = http.send(HttpRequest(method, url, headers=headers, body=body),
                                     timeout_seconds=settings.timeout)
                break
            except HttpTransportError as exc:
                error_type = transport_error_type(exc)
                self._note_transport(context, metadata, step, method, error_type, attempt,
                                     1 + settings.transport_retries)
                if method != "GET":
                    raise _PostTransport(error_type) from None
                if attempt >= attempts:
                    raise _Stop(self._transport_failed(task, step, metadata)) from None
                self._backoff(settings, attempt)
        metadata[f"{step}_http_status"] = response.status
        if response.status in {401, 403}:
            raise _Stop(self._human(context, task, "session_expired", metadata))
        if response.status in {301, 302, 303, 307, 308}:
            location = response.header("location") or ""
            if "login" in location.lower():
                raise _Stop(self._human(context, task, "session_expired", metadata))
            raise _Stop(self._failed(task, f"{step}_unexpected_redirect", metadata))
        if response.status != 200:
            raise _Stop(self._failed(task, f"{step}_http_error", metadata,
                                     next_action_at=_retry_after(response, now)))
        return response

    def _api(self, context, http, settings, cookie, task, method, url, step, metadata, now, *,
             body: dict | None = None) -> dict:
        """Call one /wctapi endpoint and return its ``data`` object (``{}`` when absent)."""
        raw = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        if method == "POST" and raw is None:
            raw = b""
        response = self._send(context, http, settings, cookie, task, method, url, step, metadata, now, body=raw)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise _Stop(self._failed(task, f"{step}_not_json", metadata)) from None
        if not isinstance(payload, dict):
            raise _Stop(self._failed(task, f"{step}_not_json", metadata))
        code = payload.get("code")
        message = _clean_message(payload.get("message") or payload.get("msg"))
        metadata[f"{step}_code"] = code if type(code) is int else None
        if message:
            metadata[f"{step}_message"] = message
        if code == SESSION_GONE_CODE:
            raise _Stop(self._human(context, task, "session_expired", metadata))
        if code != 0:
            raise _Stop(self._failed(task, f"{step}_rejected", metadata))
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _pause(self, settings) -> None:
        if settings.pause:
            self.sleeper(settings.pause)

    def _backoff(self, settings, attempt: int) -> None:
        # Constant on purpose: the failure is per connection, not load, so waiting longer buys nothing.
        if settings.transport_backoff:
            self.sleeper(settings.transport_backoff)

    def _transport_failed(self, task, step, metadata) -> KeepResult:
        # Safe to re-run later: every run re-reads the task list / guess/current before any POST.
        return self._failed(task, f"{step}_transport_error", {**metadata, "failure_class": FAILURE_CLASS_TRANSPORT})

    @staticmethod
    def _note_transport(context, metadata, step, method, error_type, attempt, limit) -> None:
        metadata["error_type"] = error_type
        metadata["transport_step"] = step
        metadata[f"{step}_transport_attempts"] = attempt
        # Diagnostics only: exception class and counters, never URLs with cookies, headers or bodies.
        LOG.warning("tushare_transport_error %s", json.dumps({
            "flame_id": context.flame_id, "step": step, "method": method, "error_type": error_type,
            "attempt": attempt, "max_attempts": limit,
        }))

    def _post_once_safely(self, context, http, settings, cookie, task, url, step, metadata, now, *,
                          body=None, recheck):
        """POST at most once per confirmed-not-done state.

        After a transport failure the POST may have landed, so ``recheck()`` re-reads the
        remote state first: it returns a finished KeepResult when the work is already done,
        or ``None`` to allow one more POST. Returns ``(data, None)`` or ``(None, result)``.
        """
        attempts = 1 + settings.transport_retries
        for attempt in range(1, attempts + 1):
            try:
                return self._api(context, http, settings, cookie, task, "POST", url, step, metadata, now,
                                 body=body), None
            except _PostTransport:
                metadata[f"{step}_transport_attempts"] = attempt
                if attempt >= attempts:
                    raise _Stop(self._transport_failed(task, step, metadata)) from None
                self._backoff(settings, attempt)
                done = recheck()
                if done is not None:
                    return None, done
        raise AssertionError("unreachable")

    # -- run ------------------------------------------------------------------------
    def keep(self, context: KeeperContext) -> KeepResult:
        metadata: dict[str, JsonValue] = {"keeper": "tushare"}
        try:
            settings = _Settings(context.settings)
        except ValueError as exc:
            return self._failed(context.settings.get("task"), "configuration_invalid",
                                {**metadata, "setting": str(exc)}, paused=True,
                                message="Tushare settings are invalid")
        task = settings.task
        metadata["task"] = task
        http = context.capabilities.require("http", HttpClient)
        now = context.clock.now() if context.clock is not None else context.now
        previous = context.storage.get_state(context.flame_id, context.account_id)
        previous_meta = previous.metadata if previous is not None else {}

        cookie: SessionCookie | None = None
        problem = None
        try:
            cookie = load_file(context.secrets.read_text(settings.cookie_file))
        except SecretReadError:
            problem = "cookie_unreadable"
        except CookieError:
            problem = "cookie_invalid"
        fingerprint = cookie.fingerprint if cookie is not None else problem
        metadata["import_tag"] = fingerprint

        # Still waiting on the same cookie today: no new requests, same action.
        if (previous is not None and previous.status is KeepStatus.WAITING_HUMAN
                and previous_meta.get("reason") in HUMAN_REASONS
                and previous_meta.get("import_tag") == fingerprint
                and context.business_target is not None
                and previous.last_checked_target == context.business_target):
            return self._human(context, task, str(previous_meta["reason"]),
                               {**metadata, "network": "skipped_unchanged_import"})
        if problem is not None:
            return self._human(context, task, problem, metadata)
        try:
            if task == "guess":
                return self._guess(context, http, settings, cookie, metadata, now)
            return self._daily_task(context, http, settings, cookie, metadata, now)
        except _Stop as stop:
            return stop.result

    # -- sign / view ----------------------------------------------------------------
    def _task_completed(self, context, http, settings, cookie, metadata, now, step) -> bool | None:
        data = self._api(context, http, settings, cookie, settings.task, "GET", TASKS_URL, step, metadata, now)
        items = data.get("datas")
        if not isinstance(items, list):
            metadata[f"{step}_listed"] = False
            return None
        for item in items:
            if isinstance(item, dict) and item.get("task_code") == settings.task_code:
                metadata[f"{step}_listed"] = True
                completed = item.get("is_completed")
                return completed if isinstance(completed, bool) else None
        # The task list no longer names this task: record it, let the action endpoint decide.
        metadata[f"{step}_listed"] = False
        return None

    def _daily_task(self, context, http, settings, cookie, metadata, now) -> KeepResult:
        task = settings.task
        metadata["task_code"] = settings.task_code
        state_key = "sign_state" if task == "sign" else "view_state"
        if self._task_completed(context, http, settings, cookie, metadata, now, "tasks") is True:
            return self._success(task, f"Tushare {_LABELS[task]} already completed today",
                                 {**metadata, state_key: "already_completed"}, now, claimed=False)
        self._pause(settings)
        if task == "view":
            # The web UI opens the ChangeLog page before calling /view.
            # TODO(T0): confirm whether the server requires this visit or only the /view call.
            self._send(context, http, settings, cookie, task, "GET", NEW_DATA_PAGE_URL, "page", metadata, now,
                       page=True)
            self._pause(settings)
        step = "sign" if task == "sign" else "view"

        def recheck():
            if self._task_completed(context, http, settings, cookie, metadata, now, "recheck") is True:
                # Not done before this run's POST, done now: the lost-response POST landed.
                return self._success(task, f"Tushare {_LABELS[task]} completed (response was lost in transit)",
                                     {**metadata, state_key: "completed_after_transport_error"}, now, claimed=True)
            return None

        data, done = self._post_once_safely(context, http, settings, cookie, task,
                                            SIGN_URL if task == "sign" else VIEW_URL, step, metadata, now,
                                            recheck=recheck)
        if done is not None:
            return done
        message = _clean_message(data.get("message")) or metadata.get(f"{step}_message")
        success = data.get("success")
        metadata["remote_success"] = success if isinstance(success, bool) else None
        if success is False:
            if isinstance(message, str) and _ALREADY.search(message):
                return self._success(task, message, {**metadata, state_key: "already_completed"}, now, claimed=False)
            return self._failed(task, f"{step}_not_successful", metadata)
        if success is not True and not settings.verify:
            # TODO(T0): /view's success shape is unverified; without verification we do not guess.
            return self._failed(task, f"{step}_unrecognized_response", metadata)
        if settings.verify:
            self._pause(settings)
            confirmed = self._task_completed(context, http, settings, cookie, metadata, now, "verify")
            metadata["remote_completed"] = confirmed
            if confirmed is False or (confirmed is None and success is not True):
                # The action endpoint answered but the task list does not show the points.
                return self._failed(task, f"{step}_not_credited", metadata)
        return self._success(task, message or f"Tushare {_LABELS[task]} succeeded",
                             {**metadata, state_key: "completed_now"}, now, claimed=True)

    # -- guess ----------------------------------------------------------------------
    def _guess(self, context, http, settings, cookie, metadata, now) -> KeepResult:
        data = self._api(context, http, settings, cookie, "guess", "GET", GUESS_CURRENT_URL, "current", metadata, now)
        period = data.get("period")
        guess = data.get("user_guess")
        if not isinstance(period, dict):
            return self._failed("guess", "guess_period_missing", metadata)
        guess = guess if isinstance(guess, dict) else {}
        status = period.get("status")
        trade_date = period.get("trade_date")
        metadata.update({
            "trade_date": trade_date if isinstance(trade_date, str) else None,
            "period_status": status if type(status) is int else None,
            "up_votes": _count(period.get("up_votes")), "down_votes": _count(period.get("down_votes")),
        })
        existing = guess.get("vote")
        if existing not in (None, "", 0):
            return self._success("guess", "Tushare guess already voted for this period",
                                 {**metadata, "guess_state": "already_voted",
                                  "vote": existing if type(existing) is int else None}, now, claimed=False)
        if status != 1:
            # 2 = waiting for the draw, 3 = drawn. Nothing can be voted until the next period opens.
            return self._success("guess", "Tushare guess is not open; nothing to vote",
                                 {**metadata, "guess_state": "not_open"}, now, claimed=False)
        try:
            vote, tied = majority_vote(period.get("up_votes"), period.get("down_votes"), settings.guess_tie,
                                       self.choose)
        except ValueError:
            return self._failed("guess", "guess_votes_unreadable", metadata)
        direction = "up" if vote == VOTE_UP else "down"
        metadata.update({"vote": vote, "vote_direction": direction, "tie": tied,
                         "tie_policy": settings.guess_tie if tied else None})
        # One line per actual vote: direction, counts and tie flag. Never the cookie.
        LOG.info("tushare_guess_vote %s", json.dumps({
            "flame_id": context.flame_id, "trade_date": metadata["trade_date"], "vote": vote,
            "direction": direction, "up_votes": metadata["up_votes"], "down_votes": metadata["down_votes"],
            "tie": tied, "tie_policy": metadata["tie_policy"],
        }, ensure_ascii=False))
        self._pause(settings)

        def recheck():
            fresh = self._api(context, http, settings, cookie, "guess", "GET", GUESS_CURRENT_URL, "recheck",
                              metadata, now)
            fresh_guess = fresh.get("user_guess") if isinstance(fresh.get("user_guess"), dict) else {}
            fresh_period = fresh.get("period") if isinstance(fresh.get("period"), dict) else {}
            recorded_vote = fresh_guess.get("vote")
            if recorded_vote not in (None, "", 0):
                same = recorded_vote == vote
                return self._success("guess", "Tushare guess vote landed (response was lost in transit)" if same
                                     else "Tushare guess already voted for this period",
                                     {**metadata, "guess_state": "voted_after_transport_error" if same else "already_voted",
                                      "vote_confirmed": same}, now, claimed=same)
            if fresh_period.get("status") != 1:
                return self._success("guess", "Tushare guess closed before the vote could be confirmed",
                                     {**metadata, "guess_state": "not_open"}, now, claimed=False)
            return None  # still open and not voted: resend the same side

        result, done = self._post_once_safely(context, http, settings, cookie, "guess", GUESS_VOTE_URL, "vote",
                                              metadata, now, body={"vote": vote}, recheck=recheck)
        if done is not None:
            return done
        after = result.get("user_guess")
        recorded = after.get("vote") if isinstance(after, dict) else None
        metadata["vote_confirmed"] = recorded == vote if recorded is not None else None
        if recorded is not None and recorded != vote:
            return self._failed("guess", "guess_vote_mismatch", metadata)
        side = "看涨" if vote == VOTE_UP else "看跌"
        how = f"on a tie ({settings.guess_tie})" if tied else "with the majority"
        return self._success("guess", f"Tushare guess voted {side} {how}",
                             {**metadata, "guess_state": "voted_now"}, now, claimed=True)
