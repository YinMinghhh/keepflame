import json
from pathlib import Path

import pytest

from keepflame.cli import main
from tests.helpers import FakeHttpClient, response, write_secret
from tests.test_config import BASE_CONFIG


def config_file(tmp_path: Path, cookie_file: Path | None = None) -> Path:
    text = BASE_CONFIG.replace(
        'path = "state/keepflame.sqlite3"', 'path = "keepflame.sqlite3"'
    )
    if cookie_file is not None:
        text = text.replace(
            'cookie_file = "/run/secrets/joinquant.cookie"',
            f'cookie_file = "{cookie_file}"',
        )
    path = tmp_path / "keepflame.toml"
    path.write_text(text)
    return path


def test_status_json_includes_configured_not_run_flame(tmp_path: Path, capsys) -> None:
    exit_code = main(["status", "--config", str(config_file(tmp_path)), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["service_ok"] is True
    assert payload["flames"][0]["status"] == "NOT_RUN"
    assert "Cookie" not in captured.out


def test_health_checks_only_local_config_and_storage(tmp_path: Path, capsys) -> None:
    exit_code = main(["health", "--config", str(config_file(tmp_path))])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["service_ok"] is True


def test_missing_config_returns_safe_error(tmp_path: Path, capsys) -> None:
    exit_code = main(["health", "--config", str(tmp_path / "missing.toml")])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert exit_code == 1
    assert payload["event"] == "command_failed"
    assert payload["error_type"] == "ConfigError"


@pytest.mark.parametrize(
    ("payload", "expected_exit", "expected_status"),
    [
        (
            '{"code":"00000","data":{"taskList":{"login":{"status":1}}}}',
            0,
            "SUCCESS",
        ),
        (
            '{"code":"00000","data":{"taskList":{"login":{"status":0}}}}',
            2,
            "WAITING_HUMAN",
        ),
        ('{"code":"20000","msg":"invalid"}', 1, "FAILED"),
    ],
)
def test_run_once_exit_codes(
    tmp_path: Path,
    capsys,
    monkeypatch,
    payload: str,
    expected_exit: int,
    expected_status: str,
) -> None:
    cookie = write_secret(tmp_path / "joinquant.cookie", "sid=test-session")
    fake_http = FakeHttpClient(response(200, payload))
    monkeypatch.setattr("keepflame.runtime.UrllibHttpClient", lambda: fake_http)
    exit_code = main(
        ["run-once", "--execute", "--config", str(config_file(tmp_path, cookie)), "--flame", "joinquant-primary"]
    )
    result = json.loads(capsys.readouterr().out)
    assert exit_code == expected_exit
    assert result["status"] == expected_status
