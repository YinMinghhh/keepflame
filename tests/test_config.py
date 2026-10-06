from pathlib import Path

import pytest

from keepflame.core.config import ConfigError, load_config


BASE_CONFIG = """
[storage]
path = "state/keepflame.sqlite3"

[[flames]]
id = "joinquant-primary"
keeper = "joinquant"
account_id = "primary"
enabled = true

[flames.schedule]
timezone = "Asia/Shanghai"
first_check = "09:00"
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"

[flames.settings]
cookie_file = "/run/secrets/joinquant.cookie"
"""


def write_config(tmp_path: Path, text: str = BASE_CONFIG) -> Path:
    path = tmp_path / "keepflame.toml"
    path.write_text(text)
    return path


def test_loads_strict_config_and_resolves_storage_path(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert config.storage.path == tmp_path / "state/keepflame.sqlite3"
    assert config.flames[0].settings["cookie_file"] == "/run/secrets/joinquant.cookie"
    assert config.flames[0].schedule.timezone == "Asia/Shanghai"


def test_duplicate_flame_id_is_rejected(tmp_path: Path) -> None:
    duplicate = BASE_CONFIG + BASE_CONFIG.split("[[flames]]", 1)[1].join(
        ["\n[[flames]]", ""]
    )
    with pytest.raises(ConfigError, match="duplicate flame id"):
        load_config(write_config(tmp_path, duplicate))


def test_invalid_schedule_order_is_rejected(tmp_path: Path) -> None:
    invalid = BASE_CONFIG.replace('reminder_at = "20:00"', 'reminder_at = "08:00"')
    with pytest.raises(ConfigError, match="first_check < reminder_at < stop_at"):
        load_config(write_config(tmp_path, invalid))


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    invalid = BASE_CONFIG.replace("enabled = true", "enabled = true\nenabeld = true")
    with pytest.raises(ConfigError, match=r"unknown flames\[0\] keys"):
        load_config(write_config(tmp_path, invalid))
