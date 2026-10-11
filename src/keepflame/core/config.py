"""Strict TOML configuration loading."""

from __future__ import annotations

import os
import hashlib
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from keepflame.core.models import JsonValue

DEFAULT_CONFIG_PATH = "/etc/keepflame/config.toml"


class ConfigError(ValueError):
    """Raised when runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class StorageConfig:
    path: Path


@dataclass(frozen=True, slots=True)
class EmailConfig:
    """Optional SMTP delivery. Disabled configs never open a socket or secret."""

    enabled: bool
    host: str
    port: int
    security: str
    username: str
    password_file: Path
    sender: str
    recipients: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class SlackConfig:
    """Optional Slack Incoming Webhook delivery.

    Only the absolute path of the file holding the webhook address is kept here.
    The address itself is read at send time and never stored on this object.
    """

    enabled: bool
    webhook_file: Path
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    timezone: str
    first_check: time
    recheck_minutes: int
    reminder_at: time
    stop_at: time

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"unknown timezone: {self.timezone}") from exc
        if self.recheck_minutes <= 0:
            raise ConfigError("recheck_minutes must be positive")
        if not self.first_check < self.reminder_at < self.stop_at:
            raise ConfigError("schedule times must satisfy first_check < reminder_at < stop_at")


@dataclass(frozen=True, slots=True)
class FlameConfig:
    id: str
    keeper: str
    account_id: str
    enabled: bool
    schedule: ScheduleConfig
    settings: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class AppConfig:
    storage: StorageConfig
    flames: tuple[FlameConfig, ...]
    source_path: Path
    source_fingerprint: str | None = None
    email: EmailConfig | None = None
    slack: SlackConfig | None = None

    def enabled_flames(self) -> tuple[FlameConfig, ...]:
        return tuple(flame for flame in self.flames if flame.enabled)


def _expect_table(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _reject_unknown(table: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown {name} keys: {', '.join(unknown)}")


def _required_text(table: dict[str, Any], key: str, name: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name}.{key} must be a non-empty string")
    return value.strip()


def _single_line(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(character.isspace() for character in value):
        raise ConfigError(f"{name} must be a single-line value")
    return value.strip()


def _parse_time(value: Any, name: str) -> time:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be HH:MM") from exc
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise ConfigError(f"{name} must be an unzoned HH:MM value")
    return parsed


def _validate_json_value(value: Any, name: str) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_validate_json_value(item, f"{name}[]") for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            key: _validate_json_value(child, f"{name}.{key}")
            for key, child in value.items()
        }
    raise ConfigError(f"{name} must contain only JSON-compatible values")


def _notify_table(raw: dict[str, Any]) -> dict[str, Any] | None:
    if "notify" not in raw:
        return None
    notify = _expect_table(raw.get("notify"), "notify")
    _reject_unknown(notify, {"email", "slack"}, "notify")
    return notify


def _parse_email(raw: dict[str, Any]) -> EmailConfig | None:
    notify = _notify_table(raw)
    if notify is None or "email" not in notify:
        return None
    table = _expect_table(notify.get("email"), "notify.email")
    _reject_unknown(
        table,
        {
            "enabled",
            "host",
            "port",
            "security",
            "username",
            "password_file",
            "from",
            "to",
            "timeout_seconds",
        },
        "notify.email",
    )
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("notify.email.enabled must be true or false")
    port = table.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("notify.email.port must be an integer from 1 to 65535")
    security = _single_line(table.get("security"), "notify.email.security")
    if security not in {"starttls", "ssl", "none"}:
        raise ConfigError("notify.email.security must be starttls, ssl, or none")
    timeout = table.get("timeout_seconds", 15)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 120:
        raise ConfigError("notify.email.timeout_seconds must be a number from 1 to 120")
    recipients_raw = table.get("to")
    if not isinstance(recipients_raw, list) or not recipients_raw:
        raise ConfigError("notify.email.to must list at least one address")
    recipients = tuple(
        _single_line(item, f"notify.email.to[{index}]")
        for index, item in enumerate(recipients_raw)
    )
    password_raw = table.get("password_file")
    if not isinstance(password_raw, str) or not password_raw.strip() or any(
        character in password_raw for character in "\r\n"
    ):
        raise ConfigError("notify.email.password_file must be a single-line absolute path")
    password_path = Path(password_raw.strip()).expanduser()
    if not password_path.is_absolute():
        raise ConfigError("notify.email.password_file must be an absolute path")
    return EmailConfig(
        enabled=enabled,
        host=_single_line(table.get("host"), "notify.email.host"),
        port=port,
        security=security,
        username=_single_line(table.get("username"), "notify.email.username"),
        password_file=password_path,
        sender=_single_line(table.get("from"), "notify.email.from"),
        recipients=recipients,
        timeout_seconds=float(timeout),
    )


def _repository_root() -> Path | None:
    """Source checkout that contains this module, or None for an installed package."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "keepflame").is_dir():
            return parent
    return None


def _parse_slack(raw: dict[str, Any]) -> SlackConfig | None:
    notify = _notify_table(raw)
    if notify is None or "slack" not in notify:
        return None
    table = _expect_table(notify.get("slack"), "notify.slack")
    _reject_unknown(table, {"enabled", "webhook_file", "timeout_seconds"}, "notify.slack")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("notify.slack.enabled must be true or false")
    timeout = table.get("timeout_seconds", 15)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 120:
        raise ConfigError("notify.slack.timeout_seconds must be a number from 1 to 120")
    file_raw = table.get("webhook_file")
    if not isinstance(file_raw, str) or not file_raw.strip() or any(
        character in file_raw for character in "\r\n"
    ):
        raise ConfigError("notify.slack.webhook_file must be a single-line absolute path")
    file_path = Path(file_raw.strip()).expanduser()
    if not file_path.is_absolute():
        raise ConfigError("notify.slack.webhook_file must be an absolute path")
    root = _repository_root()
    if root is not None and file_path.resolve().is_relative_to(root):
        raise ConfigError("notify.slack.webhook_file must be outside the repository")
    return SlackConfig(enabled=enabled, webhook_file=file_path, timeout_seconds=float(timeout))


def default_config_path() -> Path:
    return Path(os.environ.get("KEEPFLAME_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()


def load_config(path: str | Path | None = None) -> AppConfig:
    source = Path(path) if path is not None else default_config_path()
    source = source.expanduser().resolve()
    try:
        with source.open("rb") as handle:
            source_bytes = handle.read(1_048_577)
        if len(source_bytes) > 1_048_576:
            raise ConfigError("configuration exceeds size limit")
        raw = tomllib.loads(source_bytes.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {source}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {source}: {exc}") from exc

    _reject_unknown(raw, {"storage", "flames", "notify"}, "top-level")
    storage_raw = _expect_table(raw.get("storage"), "storage")
    _reject_unknown(storage_raw, {"path"}, "storage")
    storage_text = _required_text(storage_raw, "path", "storage")
    storage_path = Path(storage_text).expanduser()
    if not storage_path.is_absolute():
        storage_path = (source.parent / storage_path).resolve()

    flames_raw = raw.get("flames")
    if not isinstance(flames_raw, list) or not flames_raw:
        raise ConfigError("at least one [[flames]] entry is required")

    flames: list[FlameConfig] = []
    seen_ids: set[str] = set()
    seen_tasks: set[tuple[str, str, str]] = set()
    for index, item in enumerate(flames_raw):
        name = f"flames[{index}]"
        table = _expect_table(item, name)
        _reject_unknown(
            table,
            {"id", "keeper", "account_id", "enabled", "schedule", "settings"},
            name,
        )
        flame_id = _required_text(table, "id", name)
        if flame_id in seen_ids:
            raise ConfigError(f"duplicate flame id: {flame_id}")
        seen_ids.add(flame_id)

        enabled = table.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{name}.enabled must be true or false")

        schedule_raw = _expect_table(table.get("schedule"), f"{name}.schedule")
        _reject_unknown(
            schedule_raw,
            {"timezone", "first_check", "recheck_minutes", "reminder_at", "stop_at"},
            f"{name}.schedule",
        )
        recheck = schedule_raw.get("recheck_minutes")
        if not isinstance(recheck, int) or isinstance(recheck, bool):
            raise ConfigError(f"{name}.schedule.recheck_minutes must be an integer")
        schedule = ScheduleConfig(
            timezone=_required_text(schedule_raw, "timezone", f"{name}.schedule"),
            first_check=_parse_time(
                schedule_raw.get("first_check"), f"{name}.schedule.first_check"
            ),
            recheck_minutes=recheck,
            reminder_at=_parse_time(
                schedule_raw.get("reminder_at"), f"{name}.schedule.reminder_at"
            ),
            stop_at=_parse_time(schedule_raw.get("stop_at"), f"{name}.schedule.stop_at"),
        )

        settings_raw = _expect_table(table.get("settings", {}), f"{name}.settings")
        settings = {
            key: _validate_json_value(value, f"{name}.settings.{key}")
            for key, value in settings_raw.items()
        }
        task = settings.get("task", "checkin")
        if not isinstance(task, str) or not task:
            raise ConfigError(f"{name}.settings.task must be a non-empty string")
        identity = (_required_text(table, "keeper", name), _required_text(table, "account_id", name), task)
        if identity in seen_tasks:
            raise ConfigError("duplicate keeper/account/task configuration")
        seen_tasks.add(identity)
        flames.append(
            FlameConfig(
                id=flame_id,
                keeper=_required_text(table, "keeper", name),
                account_id=_required_text(table, "account_id", name),
                enabled=enabled,
                schedule=schedule,
                settings=settings,
            )
        )

    return AppConfig(
        storage=StorageConfig(path=storage_path),
        flames=tuple(flames),
        source_path=source,
        source_fingerprint=hashlib.sha256(source_bytes).hexdigest(),
        email=_parse_email(raw),
        slack=_parse_slack(raw),
    )

