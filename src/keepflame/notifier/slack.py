"""Optional Slack Incoming Webhook delivery for one already-decided lifecycle notification."""

from __future__ import annotations

import errno
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from keepflame.capabilities.secrets import SecretReadError
from keepflame.core.config import SlackConfig
from keepflame.core.models import KeepResult
from keepflame.core.protocols import SecretResolver

WEBHOOK_PREFIX = "https://hooks.slack.com/"
_FIELD_LIMIT = 300
_PROXY_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ENETUNREACH,
}


class SlackDeliveryError(RuntimeError):
    """Slack delivery failed. The message never includes the webhook address or a response.

    ``category`` is a safe token (timeout, proxy, http_<status>, other, or a
    webhook-file problem). The causing exception stays on ``__cause__``. Its
    text is not copied here: a transport error can contain the webhook URL.
    """

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


def render_lifecycle_message(
    *,
    event: str,
    notification_id: str,
    flame_id: str,
    account_id: str,
    result: KeepResult,
) -> str:
    """Return a short plain-text message. Metadata, URLs and page content are not serialized."""
    lines = [
        f"KeepFlame {_field(flame_id)} {_field(event)}",
        f"Flame: {_field(flame_id)}",
        f"Account: {_field(account_id)}",
        f"Event: {_field(event)}",
        f"Status: {_field(result.status.value)}",
        f"Message: {_field(result.message)}",
        f"Next-Action: {_next_action(result.next_action_at)}",
    ]
    action = result.human_action
    if action is not None:
        lines.extend([
            f"Human-Action: {_field(action.kind)}",
            f"Summary: {_field(action.summary)}",
            f"Instructions: {_field(action.instructions)}",
        ])
        link = _plain_action_url(action.url)
        if link is not None:
            lines.append(f"Action-URL: {link}")
    if event == "credit_recorded":
        credit = _credit_fields(result)
        if credit is not None:
            amount, item = credit
            lines.extend([f"Credit-Amount: {amount}", f"Credit-Item: {_field(item)}"])
    lines.append(f"Notification: {_field(notification_id)}")
    return "\n".join(lines)


def validated_webhook(raw: str) -> str:
    """Return the address if it is a Slack webhook; otherwise raise without echoing it."""
    url = raw.strip()
    if not url.startswith(WEBHOOK_PREFIX) or len(url) == len(WEBHOOK_PREFIX):
        raise SlackDeliveryError(
            "slack webhook address is not an allowed Slack address", category="invalid_address",
        )
    if any(character.isspace() or character == "\\" for character in url):
        raise SlackDeliveryError(
            "slack webhook address is not an allowed Slack address", category="invalid_address",
        )
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname == "hooks.slack.com"
            and parts.port is None
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        valid = False
    if not valid:
        raise SlackDeliveryError(
            "slack webhook address is not an allowed Slack address", category="invalid_address",
        )
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post_webhook(url: str, payload: bytes, timeout: float) -> int:
    """POST JSON once, without following redirects. Returns the HTTP status."""
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return int(response.status)


def deliver(
    config: SlackConfig,
    secrets: SecretResolver,
    text: str,
    *,
    post=None,
) -> None:
    """Read the webhook address, post one message, and raise without echoing it."""
    try:
        raw = secrets.read_text(str(config.webhook_file))
    except SecretReadError as exc:
        raise SlackDeliveryError("slack webhook file is unreadable", category="unreadable") from exc
    if raw.strip() == "":
        raise SlackDeliveryError("slack webhook file is empty", category="empty")
    url = validated_webhook(raw)
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    sender = post or post_webhook
    try:
        status = sender(url, payload, config.timeout_seconds)
    except SlackDeliveryError:
        raise
    except urllib.error.HTTPError as exc:
        raise SlackDeliveryError(
            "slack delivery was rejected", category=http_category(exc.code),
        ) from exc
    except Exception as exc:
        raise SlackDeliveryError(
            "slack delivery failed", category=transport_category(exc),
        ) from exc
    if status != 200:
        raise SlackDeliveryError("slack delivery was rejected", category=http_category(status))


def http_category(code: object) -> str:
    """``http_503`` for a real status code. Anything else stays ``http_status``."""
    if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
        return f"http_{code}"
    return "http_status"


def transport_category(exc: BaseException) -> str:
    """Classify a transport failure without returning its text.

    ``timeout`` wins over ``proxy``. ``proxy`` is a tunnel/proxy message, or a
    connection failure while an HTTP(S) proxy is configured (the Clash-down
    shape). Otherwise ``other``.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError):
            return http_category(current.code)
        if _is_timeout(current):
            return "timeout"
        if _is_proxy(current):
            return "proxy"
        current = current.__cause__
    return "other"


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ETIMEDOUT:
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException) and reason is not exc:
        return _is_timeout(reason)
    return isinstance(reason, str) and "timed out" in reason.lower()


def _is_proxy(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    parts = [type(exc).__name__, str(exc)]
    if isinstance(reason, BaseException):
        parts.append(type(reason).__name__)
        parts.append(str(reason))
    elif reason is not None:
        parts.append(str(reason))
    text = " ".join(parts).lower()
    if "proxy" in text or "tunnel connection" in text:
        return True
    return _proxy_configured() and _is_connection_failure(exc, reason)


def _is_connection_failure(exc: BaseException, reason: object) -> bool:
    for item in (exc, reason):
        if isinstance(item, (ConnectionError, ConnectionRefusedError, ConnectionResetError)):
            return True
        if isinstance(item, OSError) and item.errno in _PROXY_ERRNOS:
            return True
    return False


def _proxy_configured() -> bool:
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return False
    return any(proxies.get(name) for name in ("http", "https", "all"))


class SlackNotifier:
    def __init__(self, config: SlackConfig, secrets: SecretResolver, *, post=None) -> None:
        self.config = config
        self.secrets = secrets
        self.post = post

    def notify(
        self,
        event: str,
        *,
        notification_id: str,
        flame_id: str,
        account_id: str,
        result: KeepResult,
        occurred_at: datetime,
    ) -> None:
        text = render_lifecycle_message(
            event=event,
            notification_id=notification_id,
            flame_id=flame_id,
            account_id=account_id,
            result=result,
        )
        deliver(self.config, self.secrets, text, post=self.post)


def _field(value: object) -> str:
    """One short line with Slack control characters neutralised."""
    text = " ".join(str(value).split())
    if len(text) > _FIELD_LIMIT:
        text = text[: _FIELD_LIMIT - 1] + "…"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_URL_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
_URL_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]{1,48}){0,4}/?")


def _plain_action_url(url: str | None) -> str | None:
    """A clickable link only for a bare https origin+path; anything with a query,
    fragment, credentials or port could carry a token, so it stays out of Slack."""
    if not url or len(url) > 200:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if (parts.scheme != "https" or port is not None or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "?" in url or "#" in url or not _URL_HOST.fullmatch(host)
            or parts.netloc != host or not _URL_PATH.fullmatch(parts.path)):
        return None
    return f"https://{host}{parts.path}"


def _next_action(value: datetime | None) -> str:
    if value is None:
        return "none"
    return value.astimezone(timezone.utc).isoformat()


def _credit_fields(result: KeepResult) -> tuple[int, str] | None:
    line = result.metadata.get("credit_line")
    if not isinstance(line, dict):
        return None
    amount, item = line.get("amount"), line.get("item")
    if type(amount) is not int or isinstance(amount, bool) or amount <= 0:
        return None
    if not isinstance(item, str) or not item:
        return None
    return amount, item
