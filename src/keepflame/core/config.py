"""Strict TOML configuration loading."""

from __future__ import annotations

import os
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


def default_config_path() -> Path:
    return Path(os.environ.get("KEEPFLAME_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()


def load_config(path: str | Path | None = None) -> AppConfig:
    source = Path(path) if path is not None else default_config_path()
    source = source.expanduser().resolve()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {source}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {source}: {exc}") from exc

    _reject_unknown(raw, {"storage", "flames"}, "top-level")
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
    )

