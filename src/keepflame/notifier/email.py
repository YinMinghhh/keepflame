"""Optional SMTP delivery for one already-decided lifecycle notification."""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from keepflame.capabilities.secrets import SecretReadError
from keepflame.core.config import EmailConfig
from keepflame.core.models import KeepResult
from keepflame.core.protocols import SecretResolver


class MailDeliveryError(RuntimeError):
    """SMTP delivery failed. The message never includes the mailbox password."""


def render_lifecycle_message(
    *,
    event: str,
    notification_id: str,
    flame_id: str,
    account_id: str,
    result: KeepResult,
    occurred_at: datetime,
) -> tuple[str, str]:
    """Return a short subject and body. Metadata is not serialized."""
    subject = " ".join(f"KeepFlame {flame_id} {event}".split())
    lines = [
        f"Flame: {flame_id}",
        f"Account: {account_id}",
        f"Event: {event}",
        f"Status: {result.status.value}",
        f"Message: {result.message}",
        f"Occurred: {occurred_at.astimezone(timezone.utc).isoformat()}",
        f"Next-Action: {_next_action(result.next_action_at)}",
        f"Notification: {notification_id}",
    ]
    action = result.human_action
    if action is not None:
        lines.extend([
            f"Human-Action: {action.kind}",
            f"Summary: {action.summary}",
            f"Instructions: {action.instructions}",
        ])
        if action.url:
            lines.append(f"URL: {action.url}")
    if event == "credit_recorded":
        credit = _credit_fields(result)
        if credit is not None:
            amount, item, stamp = credit
            lines.extend([
                f"Credit-Amount: {amount}",
                f"Credit-Item: {item}",
                f"Credit-Time: {stamp}",
            ])
    return subject, "\n".join(lines) + "\n"


def smtp_connect(config: EmailConfig):
    """Open one SMTP connection with the configured timeout. Caller closes it."""
    timeout = config.timeout_seconds
    if config.security == "ssl":
        return smtplib.SMTP_SSL(config.host, config.port, timeout=timeout)
    client = smtplib.SMTP(config.host, config.port, timeout=timeout)
    if config.security == "starttls":
        client.starttls()
    return client


def deliver(
    config: EmailConfig,
    secrets: SecretResolver,
    subject: str,
    body: str,
    *,
    notification_id: str,
    connect=None,
) -> None:
    """Read the password, send one message, and raise without echoing the secret."""
    try:
        raw_password = secrets.read_text(str(config.password_file))
    except SecretReadError:
        raise MailDeliveryError("smtp password file is unreadable") from None
    password = _password(raw_password)
    if password == "":
        raise MailDeliveryError("smtp password file is empty")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    token = notification_id.strip()
    if token and not any(character.isspace() for character in token) and "@" not in token:
        message["Message-ID"] = f"<{token}@keepflame.invalid>"
    message.set_content(body)
    opener = connect or smtp_connect
    try:
        client = opener(config)
    except MailDeliveryError:
        raise
    except Exception:
        raise MailDeliveryError("smtp delivery failed") from None
    try:
        try:
            client.login(config.username, password)
            client.send_message(message)
        except MailDeliveryError:
            raise
        except Exception:
            raise MailDeliveryError("smtp delivery failed") from None
    finally:
        quit_client = getattr(client, "quit", None)
        if callable(quit_client):
            try:
                quit_client()
            except Exception:
                pass


class EmailNotifier:
    def __init__(self, config: EmailConfig, secrets: SecretResolver, *, connect=None) -> None:
        self.config = config
        self.secrets = secrets
        self.connect = connect

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
        subject, body = render_lifecycle_message(
            event=event,
            notification_id=notification_id,
            flame_id=flame_id,
            account_id=account_id,
            result=result,
            occurred_at=occurred_at,
        )
        deliver(
            self.config,
            self.secrets,
            subject,
            body,
            notification_id=notification_id,
            connect=self.connect,
        )


def _next_action(value: datetime | None) -> str:
    if value is None:
        return "none"
    return value.astimezone(timezone.utc).isoformat()


def _credit_fields(result: KeepResult) -> tuple[int, str, str] | None:
    line = result.metadata.get("credit_line")
    if not isinstance(line, dict):
        return None
    amount, item, stamp = line.get("amount"), line.get("item"), line.get("at")
    if type(amount) is not int or isinstance(amount, bool) or amount <= 0:
        return None
    if not isinstance(item, str) or not item or not isinstance(stamp, str):
        return None
    return amount, item, stamp


def _password(raw: str) -> str:
    """Drop one trailing newline added by editors. Leave the remaining bytes alone."""
    if raw.endswith("\r\n"):
        return raw[:-2]
    if raw.endswith("\n"):
        return raw[:-1]
    return raw
