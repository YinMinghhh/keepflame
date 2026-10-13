"""One bounded automatic sign-in attempt, verified by the account status API."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from keepflame.capabilities.browser import BrowserUnavailable, ProfileNotAuthorized
from keepflame.capabilities.http import HttpResponse
from keepflame.core.models import HumanAction, KeepResult, KeepStatus, Reward, RewardSource
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN, SIGNIN_PAGE_URL, JoinQuantKeeper, _unique_json_object
from keepflame.flames.joinquant.matching import MatchError, solve_challenge
from keepflame.flames.joinquant.guard import CHALLENGE_PATH, READING_PATH, FlowError, SignInGuard
from keepflame.flames.joinquant.task import TaskConfigurationError, choose_article, task_from_settings
from keepflame.flames.joinquant.day import BusinessDayFence
from keepflame.core.models import CompletionReceipt
from keepflame.flames.joinquant.history import credit_evidence, fetch_credit_history


def _checkin_write_submitted(task_name, stage_counts) -> bool:
    if task_name != "checkin" or not isinstance(stage_counts, dict):
        return False
    return any(type(stage_counts.get(key)) is int and stage_counts[key] > 0
               for key in ("challenge", "answer", "claim"))


def _zero_claim_button_miss(task_name, metadata, stage_counts) -> bool:
    if task_name != "checkin" or metadata.get("reason") != "signin_button_changed":
        return False
    if metadata.get("pause_account") or metadata.get("unconfirmed_reason") in {
            "pending_different_day", "action_confirmation_rejected"}:
        return False
    if not isinstance(stage_counts, dict):
        return False
    return all(type(stage_counts.get(key)) is int and stage_counts[key] == 0
               for key in ("challenge", "answer", "claim"))


def _previous_zero_claim_released(storage, account_id, task_name, day) -> bool:
    checker = getattr(storage, "zero_claim_pending_released", None)
    if not callable(checker):
        return False
    return bool(checker("joinquant", account_id, task_name, day))


def _safe_history(page):
    try:
        return fetch_credit_history(page)
    except Exception:
        return None


def _safe_credit(before, after, task, started_at):
    try:
        return credit_evidence(before, after, task=task, started_at=started_at)
    except Exception:
        return {"proven": False, "amount": None}


SCROLL_START_SECONDS = 2.0
SCROLL_INTERVAL_SECONDS = 0.75
SCROLL_STEP_PIXELS = 400
SCROLL_STEPS = 30


class JoinQuantPage:
    def __init__(self, page, *, deadline: float, origin: str = JOINQUANT_ORIGIN, request_fetch=None, task=None, date_fence=None):
        self.page = page
        self.deadline = deadline
        if origin != JOINQUANT_ORIGIN:
            raise FlowError("unexpected_origin")
        self.origin = origin
        self.task = task or task_from_settings({})
        self.reading_entry = None
        self._entry_observations = {}
        self.guard = SignInGuard(page, deadline=deadline, request_fetch=request_fetch, task=self.task.name, date_fence=date_fence)

    def close(self):
        # Popups share this account context and must be gone before unroute.
        # If close fails, retain the gate; BrowserPool will stop the whole context.
        for page in list(self.page.context.pages):
            page.close()
        self.guard.close()

    def timeout(self, maximum=10_000):
        self.guard.check()
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise FlowError("time_budget")
        return min(remaining, maximum)

    def open(self):
        if self.task.name == "reading":
            self.guard.prepare_claim_page()
        response = self.page.goto(self.origin + "/view/user/floor?type=creditsdesc", wait_until="domcontentloaded", timeout=self.timeout())
        if response is None:
            raise FlowError("navigation_rejected")
        if response.status >= 400:
            raise FlowError("navigation_rejected", response.status, response.header_value("retry-after"))
        if urlsplit(self.page.url).path.startswith("/user/login"):
            raise FlowError("login_required", 401)
        if urlsplit(self.page.url).netloc != urlsplit(self.origin).netloc:
            raise FlowError("unexpected_origin")
        if self.task.name == "checkin":
            # The floor and credit center fetch task info after DOMContentLoaded.
            # Those reads are only admitted while this phase is still loading.
            self._wait_for_credit_reads()

    def _wait_for_credit_reads(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.guard.counts["page_status"] < 2:
            self.guard.check()
            try:
                self.page.wait_for_timeout(50)
            except Exception as exc:
                raise FlowError("signin_button_changed") from exc

    def inspect(self, now):
        return JoinQuantKeeper._interpret(self.guard.read_status(), now=now, task=self.task.name)

    def read_article(self):
        """Let the original article execute normally; do not originate telemetry."""
        self.guard.begin_reading(article_id=self.task.article_id, article_url=self.task.article_url)
        response = self.page.goto(self.task.article_url, wait_until="domcontentloaded", timeout=self.timeout())
        if response is None or response.status != 200 or self.page.url != self.task.article_url:
            raise FlowError("article_navigation_changed", response.status if response is not None else None)
        started = time.monotonic()
        until = started + self.task.wait_seconds
        mouse = getattr(self.page, "mouse", None) if self.task.scroll else None
        next_scroll, scrolls = started + SCROLL_START_SECONDS, 0
        while time.monotonic() < until:
            self.guard.check()
            if self.page.is_closed() or self.page.url != self.task.article_url:
                raise FlowError("article_navigation_changed")
            if mouse is not None and scrolls < SCROLL_STEPS and time.monotonic() >= next_scroll:
                # One slow pass toward the bottom with ordinary wheel input.
                mouse.wheel(0, SCROLL_STEP_PIXELS)
                scrolls += 1
                next_scroll += SCROLL_INTERVAL_SECONDS
            self.page.wait_for_timeout(min(250, max(1, int((until-time.monotonic())*1000))))
        self.guard.check()
        if not self.guard.article_allowed or self.guard.counts["/community/post/postView"] != 1:
            raise FlowError("reading_registration_unconfirmed")

    def _button(self):
        if self.task.name == "reading":
            cards = self.page.locator(".floor-credit-card").filter(has=self.page.get_by_text(self.task.card_title, exact=True))
            try:
                cards.first.wait_for(state="visible", timeout=self.timeout(5_000))
            except Exception as exc:
                self.guard.check()
                self._diagnose_reading_entry()
                raise FlowError("reading_card_changed") from exc
            card_count = cards.count()
            self._entry_observations["card_count"] = min(card_count, 3)
            if card_count != 1:
                self._diagnose_reading_entry()
                raise FlowError("reading_card_changed")
            button = cards.get_by_role("button", name=self.task.button, exact=True)
            try:
                button.first.wait_for(state="visible", timeout=self.timeout(5_000))
            except Exception as exc:
                self.guard.check()
                self._diagnose_reading_entry()
                raise FlowError("reading_button_changed") from exc
            claim_count = button.count()
            self._entry_observations["claim_count"] = min(claim_count, 3)
            if claim_count != 1:
                self._diagnose_reading_entry()
                raise FlowError("reading_button_changed")
            enabled = button.is_enabled()
            self._entry_observations["claim_enabled"] = enabled
            if not enabled:
                self._diagnose_reading_entry()
                raise FlowError("reading_button_changed")
            return button
        return self.page.get_by_role("button", name="签到", exact=True)

    def wait_for_checkin_button(self):
        """Click only after the page has drawn exactly one 签到 button.

        The official floor and credit center each read task info once. Those
        two reads are what produce the button. A missing button is not a claim.
        """
        button = self.page.get_by_role("button", name="签到", exact=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.guard.check()
            try:
                count = button.count()
            except Exception:
                count = 0
            if self.guard.counts["page_status"] >= 2 and count == 1:
                return button
            try:
                self.page.wait_for_timeout(50)
            except Exception as exc:
                raise FlowError("signin_button_changed") from exc
        raise FlowError("signin_button_changed")

    def _diagnose_reading_entry(self):
        """Record completed checks only: a failed renderer must never be queried.

        Even CDP Runtime.evaluate(timeout=...) can wait forever before execution
        begins. Optional evidence cannot add a protocol wait, task, thread or
        session to the failure/cleanup path. Missing fields are unobserved, not 0.
        """
        observed = {}
        for key in ("card_count", "claim_count"):
            value = self._entry_observations.get(key)
            if type(value) is int and 0 <= value <= 3:
                observed[key] = value
        enabled = self._entry_observations.get("claim_enabled")
        if type(enabled) is bool:
            observed["claim_enabled"] = enabled
        self.reading_entry = {
            "available": False,
            "reason": "task_budget_exhausted" if time.monotonic() >= self.deadline else "post_failure_read_skipped",
            "observed": observed,
        }

    def challenge(self):
        if self.task.name == "checkin":
            button = self.wait_for_checkin_button()
        else:
            button = self._button()
            if button.count() != 1:
                raise FlowError("signin_button_changed")
        self.guard.arm_challenge()
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
            self.guard.check()
            raise FlowError("challenge_not_received") from exc
        self.guard.check()
        body = response.body()
        if len(body) > 1_400_000:
            raise FlowError("challenge_size")
        try:
            payload = json.loads(body, object_pairs_hook=_unique_json_object)
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
        self.guard.arm_validation(x)
        # Challenge token is confined to per-attempt memory; never return/log/persist it.
        result = self.page.evaluate("""async ({origin, x, timeout, rule, cardTitle, buttonText}) => {
            if (location.origin !== origin) return {reason:'unexpected_origin'};
            let scope = document;
            if (rule === 'post_view') {
                const cards = [...document.querySelectorAll('.floor-credit-card')].filter(
                    c => c.querySelector('.header-title')?.textContent.trim() === cardTitle);
                if (cards.length !== 1) return {reason:'reading_card_changed'};
                scope = cards[0];
            }
            const buttons = [...scope.querySelectorAll('button')].filter(b => b.textContent.trim() === buttonText);
            if (buttons.length !== 1) return {reason:'signin_button_changed'};
            let component = buttons[0].__vue__ || buttons[0].__vue;
            let receiver = null;
            for (let depth=0; component && depth<8; depth++, component=component.$parent) {
                if (typeof component.getToken === 'function') { receiver=component; break; }
            }
            if (!receiver) return {reason:'site_callback_changed'};
            if (rule === 'post_view' && (receiver.curRuleKey !== rule || receiver.tokenType !== 'receiveCredits')) {
                return {reason:'reading_callback_changed'};
            }
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
        }""", {"origin": self.origin, "x": x, "timeout": self.timeout(), "rule": self.task.rule,
                  "cardTitle": self.task.card_title, "buttonText": self.task.button})
        self.guard.check()
        if result.get("accepted") is not True:
            # Only fixed local reason codes cross the browser boundary.
            reasons = {"unexpected_origin", "signin_button_changed", "site_callback_changed",
                       "validation_http", "validation_shape", "challenge_answer_rejected", "validation_unconfirmed",
                       "reading_card_changed", "reading_callback_changed"}
            reason = result.get("reason")
            raise FlowError(reason if reason in reasons else "validation_unconfirmed", result.get("status"), result.get("retry_after"))
        self.guard.wait_for_signin()


class JoinQuantBrowserKeeper:
    business_timezone = "Asia/Shanghai"
    def __init__(self, *, page_factory=JoinQuantPage, solver=solve_challenge, sleeper=time.sleep):
        self.page_factory = page_factory
        self.solver = solver
        self.sleep = sleeper

    @staticmethod
    def _stop(context, reason, *, human=False, status=None, retry_after=None, paused=False):
        clock = getattr(context, "clock", None)
        observed_at = clock.now() if clock is not None else context.now
        next_action = observed_at + timedelta(hours=24 if human else 6)
        if isinstance(retry_after, str) and len(retry_after) <= 128:
            remote_limit = JoinQuantKeeper._retry_after(HttpResponse(
                status=429 if status in (401, 403) else status, url=JOINQUANT_ORIGIN,
                headers={"retry-after": retry_after}, body=b"",
            ), observed_at)
            if remote_limit is not None:
                next_action = max(next_action, remote_limit)
        metadata = {"execution_mode": "browser", "reason": reason, "remote_status": status}
        if human or paused or status == 429:
            metadata["automation_paused"] = True
        if status in (401, 403) or reason in {"session_refresh_required", "login_required"}:
            metadata["pause_account"] = True
        return KeepResult(
            status=KeepStatus.WAITING_HUMAN if human else KeepStatus.FAILED,
            message="JoinQuant automatic task stopped: " + reason,
            metadata=metadata, next_action_at=next_action,
            human_action=HumanAction(
                kind="REVIEW_BROWSER_SESSION", summary="聚宽自动任务已暂停，需要检查",
                instructions="请在已授权的独立浏览器中检查登录、任务资格或未确认领取。恢复只允许重新查询；不会重置未决写操作预算。",
                url=SIGNIN_PAGE_URL,
            ) if human else None,
        )

    def _observation(self, context, result, task):
        allowed = {task.action_kind}
        if task.name == "reading":
            allowed.add("READ_OFFICIAL_ARTICLE")
        if result.status is KeepStatus.SUCCESS or (
            result.status is KeepStatus.WAITING_HUMAN and result.human_action is not None
            and result.human_action.kind in allowed
        ):
            return replace(result, metadata={**result.metadata, "execution_mode": "browser"})
        status = result.metadata.get("remote_status")
        auth = result.status is KeepStatus.WAITING_HUMAN or status in (401, 403)
        protocol = (result.metadata.get("error_type") == "protocol" or "status_shape" in result.metadata
                    or status is None and result.status is KeepStatus.FAILED)
        stopped = self._stop(context, "session_refresh_required" if auth else "status_unavailable",
                             human=auth, paused=protocol, status=status)
        return replace(stopped, metadata={**result.metadata, **stopped.metadata},
                       next_action_at=max(stopped.next_action_at, result.next_action_at or context.now))

    def _unconfirmed(self, context, observation=None, *, cause=None):
        stopped = self._stop(context, "claim_unconfirmed", human=True,
                             status=observation.metadata.get("remote_status") if observation else None)
        metadata = {**(observation.metadata if observation else {}), **stopped.metadata}
        if cause:
            metadata["unconfirmed_reason"] = cause
        return replace(stopped, metadata=metadata,
                       next_action_at=max(stopped.next_action_at, observation.next_action_at or context.now)
                       if observation else stopped.next_action_at)

    @staticmethod
    def _remember_article(context, gateway, task, day):
        """Record an article once its registration request left the page."""
        counts = getattr(getattr(gateway, "guard", None), "counts", None)
        if counts is not None and not counts.get(READING_PATH):
            return
        record = getattr(context.storage, "record_article_read", None)
        if callable(record) and task.article_id is not None:
            record("joinquant", context.account_id, task.article_id, day, context.now)

    def _run(self, context, gateway, task, progress, fence=None):
        fence = fence or BusinessDayFence(context)
        day = fence.day
        def invoke(operation, *args):
            fence.check()
            value = operation(*args)
            fence.check()
            return value
        def inspect():
            return self._observation(context, invoke(gateway.inspect, fence.check()), task)
        action = context.storage.get_action("joinquant", context.account_id, task.name)
        if task.name == "checkin":
            invoke(gateway.open)
        before = inspect()

        # A previous day that sent no challenge, answer, or claim can be reserved again.
        # A submitted claim, a same-day pending, a missing count, and any archive stay blocked.
        released = False
        if action is not None and action.state == "pending" and action.day != day:
            released = _previous_zero_claim_released(context.storage, context.account_id, task.name, day)
            if not released:
                return self._unconfirmed(context, before, cause="pending_different_day")

        def complete(observation, automatic):
            observed_at = fence.check()
            issue = getattr(context, "completion_receipt", None)
            receipt = issue() if issue is not None else CompletionReceipt(fence.target, observed_at)
            # confirm_action matches today's pending row. Move a released older
            # pending onto today before that match, or the observation is rejected.
            if released and not progress["reserved"]:
                if not context.storage.reserve_action(
                        "joinquant", context.account_id, task.name, day, context.now,
                        validate_time=fence.check):
                    return self._unconfirmed(context, observation, cause="action_reservation_rejected")
                progress["reserved"] = True
            if (action is not None and action.state == "pending") or progress["reserved"]:
                if not context.storage.confirm_action("joinquant", context.account_id, task.name, fence.check(), day=day, validate_time=fence.check):
                    return self._unconfirmed(context, observation, cause="action_confirmation_rejected")
            # Confirmation validates the date inside its transaction. Once committed,
            # later cleanup/return time must not reclassify this confirmed completion.
            return replace(observation, receipt=receipt,
                           metadata={**observation.metadata, "automatic_action": automatic})
        if before.status is KeepStatus.SUCCESS:
            return complete(before, False)
        if action is not None and action.state == "pending" and not released:
            retry = getattr(context.storage, "zero_claim_pending_retryable", None)
            same_day_retry = (action.day == day and callable(retry)
                              and retry("joinquant", context.account_id, task.name, day))
            if not same_day_retry:
                return self._unconfirmed(context, before)
        if action is not None and action.state == "confirmed" and action.day == day:
            return self._unconfirmed(context, before, cause="same_day_action_consumed")
        if before.status is not KeepStatus.WAITING_HUMAN or before.human_action.kind not in {
            task.action_kind, "READ_OFFICIAL_ARTICLE" if task.name == "reading" else task.action_kind,
        }:
            return before

        def reserve(stage):
            fence.check()
            if not progress["reserved"]:
                if not context.storage.reserve_action("joinquant", context.account_id, task.name, day, context.now, validate_time=fence.check):
                    return False
                progress["reserved"] = True
            progress["action_stage"] = stage
            return True

        if task.name == "reading" and before.metadata.get("reading_state") == "needs_reading":
            if task.article_id is None:
                # Every configured article was already read. Never repeat one.
                return self._stop(context, "reading_articles_exhausted", human=True)
            # Article JavaScript can itself create eligibility. Journal before it runs.
            if not reserve("reading"):
                return self._unconfirmed(context, before, cause="action_reservation_rejected")
            try:
                invoke(gateway.read_article)
            finally:
                self._remember_article(context, gateway, task, day)
            before = inspect()
            if before.status is KeepStatus.SUCCESS:
                return complete(before, True)
            if before.metadata.get("reading_state") != "claimable":
                return self._unconfirmed(context, before, cause="reading_qualification_unconfirmed")
        if task.name == "reading":
            invoke(gateway.open)
        if task.name == "checkin":
            wait_button = getattr(gateway, "wait_for_checkin_button", None)
            if callable(wait_button):
                invoke(wait_button)
        if not reserve("claim"):
            return self._unconfirmed(context, before, cause="action_reservation_rejected")
        payload = piece = None
        try:
            try:
                payload, piece = invoke(gateway.challenge)
            except FlowError as exc:
                if exc.status is not None or exc.reason not in {"signin_button_changed", "challenge_not_received"}:
                    raise
                after = inspect()
                if after.status is KeepStatus.SUCCESS:
                    return complete(after, True)
                raise
            match = invoke(self.solver, payload, piece)
        finally:
            # Image URIs and challenge bytes are local to this attempt, never files.
            payload = piece = None
        invoke(gateway.submit, match.x)
        for attempt in range(3):
            after = inspect()
            if after.status is KeepStatus.SUCCESS:
                result = complete(after, True)
                return replace(result, metadata={**result.metadata, "match_confidence": match.confidence})
            if after.status is not KeepStatus.WAITING_HUMAN or after.human_action.kind != task.action_kind:
                return self._unconfirmed(context, after)
            if attempt < 2:
                self.sleep(0.5)
        return self._unconfirmed(context, after, cause="completion_unconfirmed")

    def keep(self, context):
        try:
            task = task_from_settings(context.settings)
        except TaskConfigurationError as exc:
            return self._stop(context, str(exc), human=True)
        if task.name == "reading":
            reader = getattr(context.storage, "read_article_ids", None)
            task = choose_article(task, reader("joinquant", context.account_id) if callable(reader) else ())
        profile = context.settings.get("browser_profile_dir")
        headless_setting = context.settings.get("browser_headless", True)
        if not isinstance(profile, str) or not profile or type(headless_setting) is not bool:
            return self._stop(context, "browser_configuration", human=True)
        deadline = time.monotonic() + 60 + (task.wait_seconds if task.name == "reading" else 0)
        fence = BusinessDayFence(context)
        gateway = None
        completed = None
        credit = {"proven": False, "amount": None}
        progress = {"reserved": False, "action_stage": "inspect"}
        try:
            browser = context.capabilities.require("browser")
            # The dedicated window stays visible. browser_headless cannot hide it.
            with browser.open(profile_dir=profile, account_id=context.account_id,
                              origin=JOINQUANT_ORIGIN, headless=False) as page:
                before_history = _safe_history(page)
                fence.check()
                options = {"deadline": deadline, "date_fence": fence}
                if task.name == "reading":
                    options["task"] = task
                gateway = self.page_factory(page, **options)
                try:
                    result = self._run(context, gateway, task, progress, fence)
                    if result.status is KeepStatus.SUCCESS and result.receipt is not None:
                        completed = result
                finally:
                    credit = _safe_credit(before_history, _safe_history(page), task.name, context.now)
                    # Close the task page before removing its gate, keeping the account
                    # browser context alive. Fakes may omit close; production never does.
                    if hasattr(gateway, "close"):
                        gateway.close()
        except ProfileNotAuthorized:
            result = self._stop(context, "profile_not_enrolled", human=True)
        except BrowserUnavailable:
            result = self._stop(context, "browser_unavailable", human=True)
        except ImportError:
            result = self._stop(context, "browser_dependencies_missing", human=True)
        except MatchError as exc:
            allowed = {"image_size", "image_format", "image_dimensions", "image_decode", "challenge_shape",
                       "unsupported_layout", "tile_offset", "piece_dimensions", "insufficient_pixels", "flat_piece", "ambiguous_match"}
            result = self._stop(context, str(exc) if str(exc) in allowed else "image_match_failed", paused=True)
        except FlowError as exc:
            result = self._stop(context, exc.reason, human=exc.status in (401, 403),
                                status=exc.status, retry_after=exc.retry_after,
                                paused=progress["reserved"] or exc.status not in (429, 502, 503, 504))
            if exc.blocked_request is not None:
                result = replace(result, metadata={**result.metadata, "blocked_request": exc.blocked_request})
        except Exception:
            result = self._stop(context, "browser_operation_failed", paused=True)
        if completed is not None and result is not completed:
            # A cleanup failure does not undo an already verified reward. Pause
            # resource reuse while preserving its receipt and daily write budget.
            result = replace(completed, metadata={**completed.metadata,
                "automation_paused": True, "reason": "browser_cleanup_failed",
                "cleanup_reason": result.metadata.get("reason", "browser_operation_failed")})
        metadata = {**result.metadata, "task": task.name, "action_stage": progress["action_stage"],
                    "browser_window": "visible", "credit_line": credit}
        if progress["reserved"] and result.status is not KeepStatus.SUCCESS:
            metadata["automation_paused"] = True
        if gateway is not None and hasattr(gateway, "guard"):
            counts = gateway.guard.counts
            failure = getattr(gateway.guard, "failure", None)
            if failure is not None and failure.blocked_request is not None:
                metadata["blocked_request"] = failure.blocked_request
            if getattr(gateway, "reading_entry", None) is not None:
                metadata["reading_entry"] = gateway.reading_entry
            metadata["write_guard_schema"] = 1
            metadata["stage_counts"] = {
                "status": counts["status"], "page_status": counts["page_status"],
                "reading_registration": counts["/community/post/postView"],
                "challenge": counts["/common/verifyCode/captchar"],
                "answer": counts["/common/verifyCode/validate"],
                "claim": counts["/credits/task/receiveCredits"],
                "blocked_algorithm_list": counts["blocked_algorithm_list"],
                "blocked_page_status": counts["blocked_page_status"],
            }
        stage_counts = metadata.get("stage_counts")
        # Nothing was submitted. Do not pause a releasable zero-claim attempt.
        # An archived pending, or a confirmation the storage rejected, stays stopped.
        # A check-in that already sent a challenge, answer, or claim stays stopped.
        submitted_checkin = _checkin_write_submitted(task.name, stage_counts)
        if (isinstance(stage_counts, dict) and type(stage_counts.get("claim")) is int
                and stage_counts["claim"] == 0 and not submitted_checkin
                and not metadata.get("pause_account")
                and metadata.get("unconfirmed_reason") not in {
                    "pending_different_day", "action_confirmation_rejected"}):
            metadata.pop("automation_paused", None)
        if _zero_claim_button_miss(task.name, metadata, stage_counts):
            metadata.pop("automation_paused", None)
            # One short retry. The failure ladder must not push this to six hours.
            metadata["same_day_retry_minutes"] = 30
            result = replace(result, next_action_at=context.now + timedelta(minutes=30))
        reward = result.reward
        if credit.get("proven") is True and type(credit.get("amount")) is int and credit["amount"] > 0:
            reward = Reward(str(credit["amount"]), "积分", RewardSource.OBSERVED)
        return replace(result, metadata=metadata, reward=reward)
