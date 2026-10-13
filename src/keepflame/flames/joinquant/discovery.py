"""Find a fresh public article when the configured list is used up.

Discovery only issues anonymous GET /post/<id> requests and reads the redirect
target. It never sends cookies, never follows the redirect, never uses the
account browser, and never registers a read or claims anything. The chosen
article still goes through the normal article-type check and reading flow.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

ORIGIN = "https://www.joinquant.com"
DETAIL = re.compile(r"/view/community/detail/[0-9a-f]{32}")
DEFAULT_SEED_ID = 81500
TOP_RESOLUTION = 16
WALK_LIMIT = 400


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Discovery:
    article_id: str | None
    top_id: int | None
    requests: int
    reason: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def anonymous_probe(article_id: int, *, timeout: float = 8.0) -> str:
    """Classify one id as article, question, missing or unknown. No cookies."""
    opener = urllib.request.OpenerDirector()
    # A refused redirect surfaces as HTTPError carrying the Location header.
    for handler in (urllib.request.HTTPSHandler(), _NoRedirect(), urllib.request.HTTPErrorProcessor(),
                    urllib.request.HTTPDefaultErrorHandler()):
        opener.add_handler(handler)
    request = urllib.request.Request(f"{ORIGIN}/post/{int(article_id)}", method="GET",
                                     headers={"User-Agent": "Mozilla/5.0"})
    try:
        response = opener.open(request, timeout=timeout)
        if response is None:
            return "unknown"
        with response:
            status, location = response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        status = exc.code
        location = exc.headers.get("Location", "") if exc.headers else ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DiscoveryError("network") from exc
    return classify(status, location)


def classify(status: int, location: str) -> str:
    if status not in (301, 302, 303, 307, 308) or not location:
        return "unknown"
    target = urlsplit(urljoin(ORIGIN + "/", location))
    if target.netloc != "www.joinquant.com":
        return "unknown"
    if target.path.startswith("/default/index/404"):
        return "missing"
    if not DETAIL.fullmatch(target.path):
        return "unknown"
    # ?type=... marks questions and other special posts; only plain articles qualify.
    return "question" if target.query or target.fragment else "article"


def discover(excluded, *, probe=anonymous_probe, start=None, max_requests=30,
             interval=0.5, sleep=time.sleep) -> Discovery:
    """Estimate the newest id, then walk back to the first unread plain article."""
    excluded = {str(item) for item in excluded}
    used = 0

    class Budget(Exception):
        pass

    def look(article_id):
        nonlocal used
        if used >= max_requests:
            raise Budget
        if used:
            sleep(interval)
        used += 1
        return probe(article_id)

    start = int(start or DEFAULT_SEED_ID)
    top = None
    try:
        if look(start) == "missing":
            lo, hi = max(1, start - 2048), start
        else:
            lo, step = start, 256
            while True:
                candidate = lo + step
                if look(candidate) == "missing":
                    hi = candidate
                    break
                lo, step = candidate, step * 2
        while hi - lo > TOP_RESOLUTION:
            middle = (lo + hi) // 2
            if look(middle) == "missing":
                hi = middle
            else:
                lo = middle
        top = lo
        for article_id in range(hi - 1, max(0, hi - 1 - WALK_LIMIT), -1):
            if str(article_id) in excluded:
                continue
            if look(article_id) == "article":
                return Discovery(str(article_id), top, used, "found")
    except Budget:
        return Discovery(None, top, used, "request_budget")
    except DiscoveryError:
        return Discovery(None, top, used, "network")
    return Discovery(None, top, used, "none_found")
