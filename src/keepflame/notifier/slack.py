"""Optional Slack Incoming Webhook delivery for one already-decided lifecycle notification."""

from __future__ import annotations

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


class SlackDeliveryError(RuntimeError):
    """Slack delivery failed. The message never includes the webhook address or a response."""


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
        raise SlackDeliveryError("slack webhook address is not an allowed Slack address")
    if any(character.isspace() or character == "\\" for character in url):
        raise SlackDeliveryError("slack webhook address is not an allowed Slack address")
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
        raise SlackDeliveryError("slack webhook address is not an allowed Slack address")
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
    except SecretReadError:
        raise SlackDeliveryError("slack webhook file is unreadable") from None
    if raw.strip() == "":
        raise SlackDeliveryError("slack webhook file is empty")
    url = validated_webhook(raw)
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    sender = post or post_webhook
    try:
        status = sender(url, payload, config.timeout_seconds)
    except SlackDeliveryError:
        raise
    except urllib.error.HTTPError:
        raise SlackDeliveryError("slack delivery was rejected") from None
    except Exception:
        raise SlackDeliveryError("slack delivery failed") from None
    if status != 200:
        raise SlackDeliveryError("slack delivery was rejected")


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
