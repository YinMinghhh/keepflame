"""The real CLI must deliver reload to the existing synthetic daemon database."""

import io
import json

from keepflame.cli import main
from keepflame.runtime import build_runtime
from tests.test_config import BASE_CONFIG, write_config


def test_reload_cli_must_not_create_a_new_database_and_miss_running_daemon(tmp_path, capsys):
    config_text = BASE_CONFIG.replace('enabled = true', 'enabled = false')
    path = write_config(tmp_path, config_text)
    runtime = build_runtime(path, stream=io.StringIO(), browser=object())
    try:
        path.write_text(config_text.replace('state/keepflame.sqlite3', 'other.sqlite3'))
        result = main(['reload', '--config', str(path), '--execute'])
        captured = capsys.readouterr()
        queued = json.loads(captured.out or captured.err)
        runtime.poll_controls()
        observed = {'cli_exit': result, 'cli_event': queued['event'],
                    'new_database_created': (tmp_path / 'other.sqlite3').exists(),
                    'old_daemon_command_count': len(runtime.storage.runtime_commands(include_finished=True)),
                    'old_daemon_configuration_blocked': runtime.configuration_blocked}
        (tmp_path / 'reload_changed_db.json').write_text(json.dumps(observed, sort_keys=True) + '\n')
        assert not observed['new_database_created'], 'reload opened a new database instead of addressing the live daemon'
        assert runtime.configuration_blocked, 'the running daemon did not validate or reject the database change'
    finally:
        runtime.close()


def test_explicit_reload_with_invalid_config_must_reach_daemon_pause(tmp_path, capsys):
    path = write_config(tmp_path, BASE_CONFIG.replace('enabled = true', 'enabled = false'))
    runtime = build_runtime(path, stream=io.StringIO(), browser=object())
    try:
        path.write_text('invalid = [')
        result = main(['reload', '--config', str(path), '--execute'])
        capsys.readouterr()
        runtime.poll_controls()
        observed = {'cli_exit': result,
                    'old_daemon_command_count': len(runtime.storage.runtime_commands(include_finished=True)),
                    'old_daemon_configuration_blocked': runtime.configuration_blocked}
        (tmp_path / 'reload_invalid_config.json').write_text(json.dumps(observed, sort_keys=True) + '\n')
        assert runtime.configuration_blocked, 'the explicit invalid reload never reached the running daemon'
    finally:
        runtime.close()


def test_restoring_config_does_not_clear_pause_until_explicit_reload(tmp_path, capsys):
    from datetime import datetime, timezone
    from keepflame.storage import SQLiteStorage
    text = BASE_CONFIG.replace('enabled = true', 'enabled = false')
    path = write_config(tmp_path, text)
    runtime = build_runtime(path, stream=io.StringIO(), browser=object())
    try:
        path.write_text('invalid = [')
        runtime.poll_controls()
        assert runtime.configuration_blocked
        path.write_text(text)
        runtime.poll_controls()
        assert runtime.configuration_blocked
        assert main(['reload','--config',str(path),'--execute']) == 0
        capsys.readouterr()
        runtime.poll_controls()
        assert not runtime.configuration_blocked
        assert runtime.storage.runtime_commands(include_finished=True)[0]['state'] == 'completed'
    finally:
        runtime.close()
