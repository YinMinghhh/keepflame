"""The real owner entry shows a visible window and refuses a second Chrome."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from keepflame.storage import SQLiteStorage

LIVE_CONFIG = Path.home() / "Library/Application Support/KeepFlame/acceptance.toml"
LIVE_PROFILE = "joinquant-primary"


def _chrome_pids(profile: Path) -> list[int]:
    marker = str(profile)
    output = subprocess.check_output(["ps", "-axww", "-o", "pid=,command="], text=True)
    pids = []
    for line in output.splitlines():
        if marker not in line or "python" in line.lower():
            continue
        if LIVE_PROFILE in line:
            continue
        pid_text, _, _command = line.strip().partition(" ")
        pids.append(int(pid_text))
    return pids


def _drain(stream, sink: list[str]) -> None:
    try:
        for line in stream:
            sink.append(line)
    except Exception:
        return


def _wait_for(lines: list[str], needle: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matched = [line for line in lines if needle in line]
        if matched:
            return matched[0]
        time.sleep(0.05)
    raise AssertionError(f"missing {needle}\n{''.join(lines)}")


def test_real_owner_entry_is_visible_and_refuses_a_second_chrome(tmp_path):
    profile = (tmp_path / "visible-owner-profile").resolve()
    assert LIVE_PROFILE not in str(profile)
    tmp_path.chmod(0o700)
    profile.mkdir(mode=0o700)
    profile.chmod(0o700)
    marker = profile / ".keepflame-profile.json"
    marker.write_text(json.dumps({"version": 1, "account_id": "synthetic", "purpose": "keepflame"}))
    marker.chmod(0o600)
    database = tmp_path / "owner.sqlite3"
    config = tmp_path / "owner.toml"
    config.write_text(
        "\n".join([
            "[storage]",
            f'path = "{database}"',
            "",
            "[[flames]]",
            'id = "visible-owner-checkin"',
            'keeper = "joinquant"',
            'account_id = "synthetic"',
            "enabled = true",
            "",
            "[flames.schedule]",
            'timezone = "Asia/Shanghai"',
            'first_check = "09:00"',
            "recheck_minutes = 60",
            'reminder_at = "20:00"',
            'stop_at = "23:00"',
            "",
            "[flames.settings]",
            'mode = "browser"',
            'task = "checkin"',
            f'browser_profile_dir = "{profile}"',
            "browser_headless = true",
            'browser_window = "visible"',
            "",
        ])
    )
    store = SQLiteStorage(database)
    store.pause("joinquant", "synthetic", "*", "owner_visible_probe", datetime.now(timezone.utc))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    command = [sys.executable, "-m", "keepflame", "run", "--execute", "--config", str(config)]
    stdout: list[str] = []
    stderr: list[str] = []
    first = subprocess.Popen(
        command, cwd=Path(__file__).resolve().parents[1], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert first.stdout is not None and first.stderr is not None
    threading.Thread(target=_drain, args=(first.stdout, stdout), daemon=True).start()
    threading.Thread(target=_drain, args=(first.stderr, stderr), daemon=True).start()
    try:
        try:
            record = json.loads(_wait_for(stdout, '"event":"visible_owner"', 45))
        except AssertionError as exc:
            raise AssertionError(f"{exc}\nstderr={''.join(stderr)}") from None
        assert record["browser"] == "visible"
        assert record["browser_window"] == "visible"
        assert record["browser_headless"] is True
        assert record["browser_launches"] == 1
        assert record["second_browser"] is False
        stable = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = _chrome_pids(profile)
            if current and current == stable:
                break
            stable = current
            time.sleep(0.4)
        chrome_after_start = _chrome_pids(profile)
        assert chrome_after_start, "visible owner did not launch Chrome for the temporary profile"
        assert all(LIVE_PROFILE not in str(profile) for _pid in chrome_after_start)
        second = subprocess.run(
            command, cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=20, check=False,
        )
        assert second.returncode == 1
        refused = json.loads(second.stdout.strip().splitlines()[-1])
        assert refused["event"] == "owner_refused"
        assert refused["browser_launches"] == 0
        assert refused["profile_in_use"] is True
        assert _chrome_pids(profile) == chrome_after_start
        print("observation visible_owner browser=visible browser_window=visible browser_headless=true browser_launches=1")
        print("observation second_start owner_refused browser_launches=0 chrome_unchanged=true")
        print("startup " + json.dumps(record, sort_keys=True, separators=(",", ":")))
        print("refused " + json.dumps(refused, sort_keys=True, separators=(",", ":")))
    finally:
        if first.poll() is None:
            os.kill(first.pid, signal.SIGTERM)
            try:
                first.wait(timeout=25)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _chrome_pids(profile):
            time.sleep(0.4)
        for pid in _chrome_pids(profile):
            os.kill(pid, signal.SIGTERM)
        time.sleep(1)

    with sqlite3.connect(database) as connection:
        runs = connection.execute("SELECT COUNT(*) FROM keep_runs").fetchone()[0]
        attempts = connection.execute("SELECT COUNT(*) FROM action_attempts").fetchone()[0]
    assert runs == 0 and attempts == 0
    assert _chrome_pids(profile) == []
    live = LIVE_CONFIG.read_text()
    assert live.count('browser_headless = true') >= 2
    assert live.count('browser_window = "visible"') >= 2
    assert LIVE_PROFILE in live
    print("observation live_acceptance browser_window=visible browser_headless=true")
