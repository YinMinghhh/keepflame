"""Request budgets for one JoinQuant sign-in, separate from interactive login."""

from collections import Counter
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from keepflame.capabilities.http import HttpRequest, HttpResponse
from keepflame.flames.joinquant.keeper import JOINQUANT_ORIGIN, TASK_INFO_URL, _unique_json_object

CHALLENGE_PATH = "/common/verifyCode/captchar"
VALIDATE_PATH = "/common/verifyCode/validate"
SIGNIN_PATH = "/credits/task/receiveCredits"
TOKEN_PATH = "/user/account/getToken"
READING_PATH = "/community/post/postView"
ARTICLE_INFO_PATH = "/community/post/detailV2"
WRITE_PATHS = {CHALLENGE_PATH, VALIDATE_PATH, SIGNIN_PATH}


class FlowError(RuntimeError):
    def __init__(self, reason, status=None, retry_after=None):
        self.reason, self.status, self.retry_after = reason, status, retry_after
        super().__init__(reason)


class SignInGuard:
    def __init__(self, page, *, deadline, request_fetch=None, task="checkin"):
        if task not in ("checkin", "reading"):
            raise FlowError("task_configuration")
        self.page, self.deadline = page, deadline
        self.task = task
        self.rule = "post_view" if task == "reading" else "login"
        self.request_fetch = request_fetch  # Synthetic transport injection, never a config option.
        self.phase = "loading"
        self.counts = Counter()
        self.failure = None
        self.axis = None
        self.signin_request = None
        self.signin_response = None
        self.article_id = self.article_url = None
        self.article_allowed = False
        self._handler = self.route
        self._closed = False
        page.context.route("**/*", self._handler)

    def close(self):
        """Remove only this run's handler; the account context belongs to its pool."""
        if not self._closed:
            self._closed = True
            self.phase = "stopped"
            self.page.context.unroute("**/*", self._handler)

    def begin_reading(self, *, article_id, article_url):
        self.check()
        if self.task != "reading" or self.phase != "inspect" or self.article_id is not None:
            raise FlowError("reading_phase_changed")
        self.article_id, self.article_url = article_id, article_url
        self.phase = "reading"

    def prepare_claim_page(self):
        self.check()
        if self.task != "reading" or self.phase != "inspect":
            raise FlowError("reading_phase_changed")
        self.phase = "loading"

    def check(self):
        if self.failure is not None:
            raise self.failure
        if time.monotonic() >= self.deadline:
            raise FlowError("time_budget")

    def stop(self, reason, status=None, retry_after=None):
        if self.failure is None:
            self.failure = FlowError(reason, status, retry_after)
        self.phase = "stopped"

    def timeout(self):
        self.check()
        return max(1, min(10_000, int((self.deadline-time.monotonic())*1000)))

    def fetch(self, route=None):
        """Use native request APIs: no page fetch, redirects, or transport retries."""
        timeout = self.timeout()
        if self.request_fetch is not None:
            source = route or SimpleNamespace(request=HttpRequest(method="GET", url=TASK_INFO_URL))
            return self.request_fetch(source, timeout=timeout)
        response = None
        try:
            if route is not None:
                response = route.fetch(timeout=timeout, max_redirects=0, max_retries=0)
            else:
                response = self.page.context.request.get(TASK_INFO_URL, timeout=timeout,
                    max_redirects=0, max_retries=0,
                    headers={"Accept":"application/json", "X-Requested-With":"XMLHttpRequest", "Cache-Control":"no-cache"})
            headers = {k.lower():v for k,v in response.headers.items()}
            # Error bodies cannot influence local status and need not be retained.
            body = response.body() if response.status == 200 else b""
            return response.status, headers, body
        finally:
            if response is not None:
                response.dispose()

    def read_status(self):
        self.check()
        if self.counts["status"] >= (5 if self.task == "reading" else 4):
            raise FlowError("status_budget")
        self.phase = "inspect"
        self.counts["status"] += 1
        try:
            status, headers, body = self.fetch()
        except Exception:
            self.stop("status_transport")
            self.check()
        self.check()
        if status != 200:
            self.stop("status_http", status, headers.get("retry-after"))
            self.check()
        if len(body) > 1_048_576:
            raise FlowError("status_size")
        return HttpResponse(status=status, url=TASK_INFO_URL, headers=headers, body=body)

    def arm_challenge(self):
        self.check()
        self.phase = "challenge"

    def arm_validation(self, axis):
        self.check()
        self.phase, self.axis = "validate", str(axis)

    @staticmethod
    def form(request):
        raw = request.post_data or ""
        content_type = request.headers.get("content-type", "").split(";",1)[0].strip().lower()
        if len(raw) > 8192 or content_type != "application/x-www-form-urlencoded":
            return None
        try:
            values = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        except ValueError:
            return None
        if any(len(v) != 1 or not v[0] or len(v[0]) > 2048 for v in values.values()):
            return None
        return values

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path, method = parsed.path, request.method
        if self.failure is not None:
            route.abort("blockedbyclient")
            return
        # Login has its own user-controlled browser phase; this guard never logs in.
        if method == "GET" and not parsed.fragment:
            if request.resource_type in {"script","stylesheet","image","font"} and origin in {
                JOINQUANT_ORIGIN, "https://cdn.joinquant.com"
            } and path.startswith("/themes/jq/static/"):
                route.fallback()
                return
            if origin == JOINQUANT_ORIGIN and path.startswith("/user/login"):
                self.stop("login_required", 401)
                route.abort("blockedbyclient")
                return
            if origin == JOINQUANT_ORIGIN and self.phase == "reading":
                if request.url == self.article_url and request.resource_type == "document" and not self.counts["article_navigation"]:
                    self.counts["article_navigation"] += 1
                    self.forward(route, "article_document", 2_097_152)
                    return
                if path == ARTICLE_INFO_PATH and parsed.query == "postId=" + self.article_id and not self.counts[ARTICLE_INFO_PATH]:
                    self.counts[ARTICLE_INFO_PATH] += 1
                    self.forward(route, "article_info", 1_048_576)
                    return
                if path == "/user/index/isLogin" and not parsed.query and self.counts["article_login_status"] < 2:
                    self.counts["article_login_status"] += 1
                    route.fallback()
                    return
                if path == TOKEN_PATH and not parsed.query:
                    if not self.article_allowed or self.counts["reading_token"]:
                        self.stop("reading_token_budget")
                    else:
                        self.counts["reading_token"] += 1
                        self.forward(route, "reading_token", 65_536)
                        return
                    route.abort("blockedbyclient")
                    return
            if origin == JOINQUANT_ORIGIN and self.phase == "loading":
                if (self.task == "reading" and path == "/credits/task/getTaskInfo" and not parsed.query
                        and self.counts["page_status"] == 0):
                    self.counts["page_status"] += 1
                    self.forward(route, "page_status", 1_048_576)
                    return
                if path == "/view/user/floor" and parsed.query == "type=creditsdesc" and self.counts["navigation"] < (2 if self.task == "reading" else 1):
                    self.counts["navigation"] += 1
                    self.forward(route, "credits_document", 2_097_152)
                    return
                if path == "/user/index/isLogin" and not parsed.query and self.counts["login_status"] < (4 if self.task == "reading" else 2):
                    self.counts["login_status"] += 1
                    route.fallback()
                    return
            if origin == JOINQUANT_ORIGIN and path == TOKEN_PATH and not parsed.query:
                if self.phase != "submit" or self.counts[TOKEN_PATH]:
                    self.stop("signin_token_budget")
                else:
                    self.counts[TOKEN_PATH] += 1
                    self.forward(route, "signin_token", 65_536)
                    return
            elif path in WRITE_PATHS or path == READING_PATH:
                self.stop("write_method_changed")
            # Page-generated reads cannot masquerade as our status confirmations.
            route.abort("blockedbyclient")
            return
        if origin != JOINQUANT_ORIGIN or parsed.query or parsed.fragment or method != "POST":
            self.stop("unexpected_write")
        elif path == READING_PATH:
            self._reading_request(route)
            return
        elif path not in WRITE_PATHS:
            self.stop("unexpected_write")
        elif self.counts[path]:
            self.stop("write_budget_exceeded")
        elif path == CHALLENGE_PATH and self.phase == "challenge" and not request.post_data:
            self.counts[path] += 1
            self.forward(route, "challenge", 1_400_000)
            return
        elif path == VALIDATE_PATH and self.phase == "validate" and self.form(request) == {"axisX":[self.axis]}:
            self.counts[path] += 1
            self.forward(route, "validation", 65_536)
            return
        elif path == SIGNIN_PATH and self.phase == "submit":
            form = self.form(request)
            if form is not None and set(form) == {"ruleKey","valideCode","appKey","appSecret"} and form["ruleKey"] == [self.rule]:
                self.counts[path] += 1
                self.signin_request = request
                self.forward(route, "signin", 65_536)
                return
            self.stop("signin_parameters_changed")
        else:
            self.stop("write_phase_changed")
        route.abort("blockedbyclient")

    def _reading_request(self, route):
        """Admit one natural official-page registration, never originate it."""
        request = route.request
        form = self.form(request)
        try:
            current_page = request.frame.page is self.page and request.frame is self.page.main_frame
            current_url = request.frame.url == self.article_url
        except Exception:
            current_page = current_url = False
        if (self.task != "reading" or self.phase != "reading" or not self.article_allowed
                or not current_page or not current_url or self.counts[READING_PATH]
                or self.counts["reading_token"] != 1 or form is None
                or set(form) != {"postId", "appKey", "appSecret"}
                or form["postId"] != [self.article_id]):
            self.stop("reading_parameters_changed")
            route.abort("blockedbyclient")
            return
        self.counts[READING_PATH] += 1
        self.forward(route, "reading_registration", 65_536)

    def forward(self, route, kind, limit):
        try:
            status, headers, body = self.fetch(route)
            self.check()
            if status != 200:
                self.stop(kind + "_http", status, headers.get("retry-after"))
                route.abort("blockedbyclient")
                return
            if len(body) > limit:
                raise FlowError(kind + "_size")
            if kind == "validation":
                payload = json.loads(body, object_pairs_hook=_unique_json_object)
                data = payload.get("data") if isinstance(payload,dict) else None
                if not isinstance(data,dict) or data.get("result") is not True or not isinstance(data.get("token"),str) or not data["token"]:
                    raise FlowError("challenge_answer_rejected")
                self.phase = "submit"
            if kind == "signin":
                payload = json.loads(body, object_pairs_hook=_unique_json_object)
                if not isinstance(payload,dict) or payload.get("code") != "00000":
                    raise FlowError("signin_response_changed")
                self.signin_response = (route.request, status)
                self.phase = "confirm"
            if kind == "article_info":
                payload = json.loads(body, object_pairs_hook=_unique_json_object)
                data = payload.get("data") if isinstance(payload, dict) and payload.get("code") == "00000" else None
                kind_value = data.get("type") if isinstance(data, dict) else None
                if (not isinstance(data, dict) or data.get("url")
                        or not ((type(kind_value) is int and kind_value in (0, 1, 2, 3, 4))
                                or (type(kind_value) is str and kind_value in ("0", "1", "2", "3", "4")))):
                    raise FlowError("article_not_supported")
                self.article_allowed = True
            if kind == "reading_registration":
                payload = json.loads(body, object_pairs_hook=_unique_json_object)
                if not isinstance(payload, dict) or payload.get("code") != "00000":
                    raise FlowError("reading_registration_rejected")
            if kind == "page_status":
                payload = json.loads(body, object_pairs_hook=_unique_json_object)
                if not isinstance(payload, dict) or payload.get("code") != "00000":
                    raise FlowError("page_status_changed")
            # Native fetch decoded the body; do not reapply compression/length headers.
            clean_headers = {k:v for k,v in headers.items() if k.lower() not in {"content-encoding","content-length"}}
            route.fulfill(status=status, headers=clean_headers, body=body)
        except FlowError as exc:
            self.stop(exc.reason, exc.status, exc.retry_after)
            route.abort("blockedbyclient")
        except Exception:
            self.stop(kind + "_transport")
            route.abort("blockedbyclient")

    def wait_for_signin(self):
        until = min(self.deadline, time.monotonic()+8)
        while self.signin_response is None:
            self.check()
            if time.monotonic() >= until:
                raise FlowError("signin_unconfirmed")
            # Pump events until the guard observes the native response, not page JS.
            self.page.wait_for_timeout(25)
        # Dispatch routing events queued with the response before confirming state.
        self.page.wait_for_timeout(min(25, self.timeout()))
        self.check()
        if self.signin_response[0] is not self.signin_request:
            raise FlowError("signin_request_changed")
