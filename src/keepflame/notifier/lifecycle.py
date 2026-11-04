"""Lifecycle delivery writes JSONL and, only for enabled channels, sends mail or Slack."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime

from keepflame.core.config import AppConfig
from keepflame.core.delivery import channel_failure_category
from keepflame.core.models import KeepResult
from keepflame.core.protocols import SecretResolver
from keepflame.notifier.email import EmailNotifier
from keepflame.notifier.jsonl import JsonlNotifier
from keepflame.notifier.slack import SlackNotifier

_DELIVERED_LIMIT = 1024


class ChannelDeliveryError(RuntimeError):
    """More than one channel failed. Names channels only, never causes or addresses.

    ``category`` is a token summary such as ``email:other,slack:timeout``.
    """

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


class ConfigView:
    """Mutable handle so a running daemon can adopt a reloaded configuration."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config


class LifecycleNotifier:
    def __init__(
        self,
        jsonl: JsonlNotifier,
        config_source: ConfigView,
        secrets: SecretResolver,
        *,
        connect=None,
        slack_post=None,
    ) -> None:
        self.jsonl = jsonl
        self.config_source = config_source
        self.secrets = secrets
        self.connect = connect
        self.slack_post = slack_post
        # Channels that already accepted a notification id. A retry caused by another
        # channel's failure skips them, so one broken channel cannot duplicate the other.
        self._delivered: OrderedDict[tuple[str, str], None] = OrderedDict()
        # Identical lifecycle JSONL for one notification id is written once per process.
        # Retries, including ``notify retry``, keep that id, so they do not append the
        # same line again here. A new process writes it once more (at-least-once).
        # ``notification_delivery_failed`` is a separate event and is not deduped.
        self._jsonl_written: OrderedDict[str, str] = OrderedDict()

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
        fields = dict(
            notification_id=notification_id,
            flame_id=flame_id,
            account_id=account_id,
            result=result,
            occurred_at=occurred_at,
        )
        # Same payload, same notification id: do not append another lifecycle line.
        # A changed result still writes. Failure categories are logged separately.
        line = self.jsonl.notification_line(event, **fields)
        if self._jsonl_written.get(notification_id) != line:
            self.jsonl.write_line(line)
            self._jsonl_written[notification_id] = line
            while len(self._jsonl_written) > _DELIVERED_LIMIT:
                self._jsonl_written.popitem(last=False)
        config = self._config()
        channels = []
        if config.email is not None and config.email.enabled:
            channels.append(("email", EmailNotifier(config.email, self.secrets, connect=self.connect)))
        if config.slack is not None and config.slack.enabled:
            channels.append(("slack", SlackNotifier(config.slack, self.secrets, post=self.slack_post)))
        failures: list[tuple[str, Exception]] = []
        for name, channel in channels:
            key = (name, notification_id)
            if key in self._delivered:
                continue
            try:
                channel.notify(event, **fields)
            except Exception as exc:
                failures.append((name, exc))
            else:
                self._delivered[key] = None
                while len(self._delivered) > _DELIVERED_LIMIT:
                    self._delivered.popitem(last=False)
        if len(failures) == 1:
            raise failures[0][1]
        if failures:
            raise ChannelDeliveryError(
                "notification delivery failed: " + ", ".join(name for name, _exc in failures),
                category=channel_failure_category(failures),
            )

    def _config(self) -> AppConfig:
        return self.config_source.config
