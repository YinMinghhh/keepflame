"""Lifecycle delivery writes JSONL and, only when mail is enabled, sends mail."""

from __future__ import annotations

from datetime import datetime

from keepflame.core.config import AppConfig
from keepflame.core.models import KeepResult
from keepflame.core.protocols import SecretResolver
from keepflame.notifier.email import EmailNotifier
from keepflame.notifier.jsonl import JsonlNotifier


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
    ) -> None:
        self.jsonl = jsonl
        self.config_source = config_source
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
        self.jsonl.notify(
            event,
            notification_id=notification_id,
            flame_id=flame_id,
            account_id=account_id,
            result=result,
            occurred_at=occurred_at,
        )
        email = self._config().email
        if email is None or not email.enabled:
            return
        EmailNotifier(email, self.secrets, connect=self.connect).notify(
            event,
            notification_id=notification_id,
            flame_id=flame_id,
            account_id=account_id,
            result=result,
            occurred_at=occurred_at,
        )

    def _config(self) -> AppConfig:
        return self.config_source.config
