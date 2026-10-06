"""One bounded automatic sign-in attempt, verified by the account status API."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import time
from urllib.parse import urlsplit

from keepflame.capabilities.browser import BrowserUnavailable, ProfileNotAuthorized
from keepflame.capabilities.http import HttpResponse
from keepflame.core.models import HumanAction, KeepResult, KeepStatus
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN, SIGNIN_PAGE_URL, JoinQuantKeeper
from keepflame.flames.joinquant.matching import MatchError, solve_challenge

CHALLENGE_PATH = "/common/verifyCode/captchar"


class FlowError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None, retry_after: str | None = None):
        self.reason = reason
        self.status = status
        self.retry_after = retry_after
        super().__init__(reason)


class JoinQuantPage:
    def __init__(self, page, *, deadline: float, origin: str = JOINQUANT_ORIGIN):
        self.page = page
        self.deadline = deadline
        self.origin = origin

    def timeout(self, maximum=10_000):
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise FlowError("time_budget")
        return min(remaining, maximum)

    def open(self):
        response = self.page.goto(self.origin + "/view/user/floor?type=creditsdesc", wait_until="domcontentloaded", timeout=self.timeout())
        if response is None:
            raise FlowError("navigation_rejected")
        if response.status >= 400:
            raise FlowError("navigation_rejected", response.status, response.header_value("retry-after"))
        if urlsplit(self.page.url).path.startswith("/user/login"):
            raise FlowError("login_required", 401)
        if urlsplit(self.page.url).netloc != urlsplit(self.origin).netloc:
            raise FlowError("unexpected_origin")

    def inspect(self, now):
        response = self.page.evaluate("""async ({origin, timeout}) => {
            if (location.origin !== origin) throw new Error('origin');
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeout);
            try {
                const r = await fetch('/credits/task/getTaskInfo', {
                    method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
                    headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'},
                    signal: controller.signal
                });
                const body = await r.text();
                if (body.length > 1048576) throw new Error('size');
                return {status:r.status, body, headers:Object.fromEntries(r.headers)};
            } finally { clearTimeout(timer); }
        }""", {"origin": self.origin, "timeout": self.timeout()})
        return JoinQuantKeeper._interpret(HttpResponse(
            status=response["status"], url=self.origin + "/credits/task/getTaskInfo",
            headers=response["headers"], body=response["body"].encode(),
        ), now=now)

    def challenge(self):
        button = self.page.get_by_role("button", name="签到", exact=True)
        if button.count() != 1:
            raise FlowError("signin_button_changed")
        def matches(request):
            return (urlsplit(request.url).path == CHALLENGE_PATH
                    and urlsplit(request.url).netloc == urlsplit(self.origin).netloc
                    and request.method == "POST")

        try:
            # Wait for the complete body with an explicit deadline. response.body()
            # itself has no timeout option and must not wait on an open stream.
            with self.page.expect_request_finished(matches, timeout=self.timeout(8_000)) as finished:
                with self.page.expect_response(lambda r: matches(r.request), timeout=self.timeout(8_000)) as received:
                    button.click(timeout=self.timeout(5_000))
                response = received.value
                if response.status != 200:
                    raise FlowError("challenge_rejected", response.status, response.header_value("retry-after"))
            if finished.value != response.request:
                raise FlowError("challenge_changed")
        except FlowError:
            raise
        except Exception as exc:
            raise FlowError("challenge_not_received") from exc
        body = response.body()
        if len(body) > 1_400_000:
            raise FlowError("challenge_size")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise FlowError("challenge_shape") from exc
        piece = self.page.locator("#yth_captchar #xy_img")
        piece.wait_for(state="visible", timeout=self.timeout(5_000))
        uri = piece.evaluate("""el => {
            const value = getComputedStyle(el).backgroundImage;
            const match = /^url\\(["']?(data:image\\/[^"')]+)["']?\\)$/.exec(value);
            return match ? match[1] : null;
        }""")
        if not uri:
            raise FlowError("piece_image_changed")
        return payload, uri

    def submit(self, x: int):
        # Challenge token remains in browser memory; never return/log/persist it.
        result = self.page.evaluate("""async ({origin, x, timeout}) => {
            if (location.origin !== origin) return {reason:'unexpected_origin'};
            const buttons = [...document.querySelectorAll('button')].filter(b => b.textContent.trim() === '签到');
            if (buttons.length !== 1) return {reason:'signin_button_changed'};
            let component = buttons[0].__vue__ || buttons[0].__vue;
            let receiver = null;
            for (let depth=0; component && depth<8; depth++, component=component.$parent) {
                if (typeof component.getToken === 'function') { receiver=component; break; }
            }
            if (!receiver) return {reason:'site_callback_changed'};
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeout);
            try {
                const response = await fetch('/common/verifyCode/validate', {
                    method:'POST', credentials:'same-origin', redirect:'error', cache:'no-store',
                    headers:{'X-Requested-With':'XMLHttpRequest','Content-Type':'application/x-www-form-urlencoded'},
                    body:new URLSearchParams({axisX:String(x)}), signal:controller.signal
                });
                if (response.status !== 200) return {reason:'validation_http', status:response.status, retry_after:response.headers.get('Retry-After')};
                const raw = await response.text();
                if (raw.length > 65536) return {reason:'validation_shape'};
                const body = JSON.parse(raw);
                if (body?.data?.result !== true || typeof body?.data?.token !== 'string' || !body.data.token) {
                    return {reason:'challenge_answer_rejected'};
                }
                receiver.getToken(body.data.token);
                return {accepted:true};
            } catch { return {reason:'validation_unconfirmed'}; }
            finally { clearTimeout(timer); }
        }""", {"origin": self.origin, "x": x, "timeout": self.timeout()})
        if result.get("accepted") is not True:
            # Only fixed local reason codes cross the browser boundary.
            reasons = {"unexpected_origin", "signin_button_changed", "site_callback_changed",
                       "validation_http", "validation_shape", "challenge_answer_rejected", "validation_unconfirmed"}
            reason = result.get("reason")
            raise FlowError(reason if reason in reasons else "validation_unconfirmed", result.get("status"), result.get("retry_after"))


class JoinQuantBrowserKeeper:
    def __init__(self, *, page_factory=JoinQuantPage, solver=solve_challenge, sleeper=time.sleep):
        self.page_factory = page_factory
        self.solver = solver
        self.sleep = sleeper

    @staticmethod
    def _stop(context, reason, *, human=False, status=None, retry_after=None):
        next_action = context.now + timedelta(hours=24 if human else 6)
        if isinstance(retry_after, str) and len(retry_after) <= 128:
            remote_limit = JoinQuantKeeper._retry_after(HttpResponse(
                status=status, url=JOINQUANT_ORIGIN, headers={"retry-after":retry_after}, body=b"",
            ), context.now)
            if remote_limit is not None:
                next_action = max(next_action, remote_limit)
        return KeepResult(
            status=KeepStatus.WAITING_HUMAN if human else KeepStatus.FAILED,
            message="JoinQuant automatic sign-in stopped: " + reason,
            metadata={"execution_mode": "browser", "reason": reason, "remote_status": status},
            next_action_at=next_action,
            human_action=HumanAction(
                kind="REVIEW_BROWSER_SESSION", summary="聚宽自动签到需要检查独立会话",
                instructions="仅在已授权的 KeepFlame 独立浏览器中更新登录或检查站点验证；不要在聊天中提供密码或 Cookie。",
                url=SIGNIN_PAGE_URL,
            ) if human else None,
        )

    def _observation(self, context, result):
        if result.status is KeepStatus.SUCCESS or (
            result.status is KeepStatus.WAITING_HUMAN
            and result.human_action is not None
            and result.human_action.kind == "COMPLETE_OFFICIAL_SIGNIN"
        ):
            return replace(result, metadata={**result.metadata, "execution_mode": "browser"})
        status = result.metadata.get("remote_status")
        human = result.status is KeepStatus.WAITING_HUMAN or status in (401, 403)
        stopped = self._stop(context, "session_refresh_required" if human else "status_unavailable",
                             human=human, status=status)
        return replace(stopped, metadata={**result.metadata, **stopped.metadata},
                       next_action_at=max(stopped.next_action_at, result.next_action_at or context.now))

    def keep(self, context):
        profile = context.settings.get("browser_profile_dir")
        headless = context.settings.get("browser_headless", True)
        if not isinstance(profile, str) or not profile or type(headless) is not bool:
            return self._stop(context, "browser_configuration", human=True)
        deadline = time.monotonic() + 60
        try:
            browser = context.capabilities.require("browser")
            with browser.open(profile_dir=profile, account_id=context.account_id,
                              origin=JOINQUANT_ORIGIN, headless=headless) as page:
                gateway = self.page_factory(page, deadline=deadline)
                gateway.open()
                before = self._observation(context, gateway.inspect(context.now))
                if before.status is KeepStatus.SUCCESS:
                    return replace(before, metadata={**before.metadata, "execution_mode": "browser", "automatic_action": False})
                if before.status is not KeepStatus.WAITING_HUMAN or before.human_action.kind != "COMPLETE_OFFICIAL_SIGNIN":
                    return before
                try:
                    payload, piece = gateway.challenge()
                except FlowError as exc:
                    if exc.status is not None:
                        raise
                    # Some site versions can complete without a challenge. A missing
                    # widget/button alone is never interpreted as success.
                    after = self._observation(context, gateway.inspect(context.now))
                    if after.status is KeepStatus.SUCCESS:
                        return replace(after, metadata={**after.metadata, "execution_mode": "browser", "automatic_action": True})
                    raise
                match = self.solver(payload, piece)
                if time.monotonic() >= deadline:
                    raise FlowError("time_budget")
                gateway.submit(match.x)
                for attempt in range(3):
                    after = self._observation(context, gateway.inspect(context.now))
                    if after.status is KeepStatus.SUCCESS:
                        return replace(after, metadata={**after.metadata, "execution_mode": "browser",
                            "automatic_action": True, "match_confidence": match.confidence})
                    if after.status is not KeepStatus.WAITING_HUMAN or after.human_action.kind != "COMPLETE_OFFICIAL_SIGNIN":
                        return after
                    if attempt < 2:
                        self.sleep(0.5)
                return self._stop(context, "completion_unconfirmed")
        except ProfileNotAuthorized:
            return self._stop(context, "profile_not_enrolled", human=True)
        except BrowserUnavailable:
            return self._stop(context, "browser_unavailable", human=True)
        except ImportError:
            return self._stop(context, "browser_dependencies_missing", human=True)
        except MatchError as exc:
            allowed = {"image_size", "image_format", "image_dimensions", "image_decode", "challenge_shape",
                       "unsupported_layout", "tile_offset", "piece_dimensions", "insufficient_pixels", "flat_piece", "ambiguous_match"}
            return self._stop(context, str(exc) if str(exc) in allowed else "image_match_failed")
        except FlowError as exc:
            return self._stop(context, exc.reason, human=exc.status in (401, 403), status=exc.status, retry_after=exc.retry_after)
        except Exception:
            return self._stop(context, "browser_operation_failed")
