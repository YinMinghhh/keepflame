"""Command-line interface for KeepFlame."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from keepflame.core.config import ConfigError, FlameConfig, default_config_path
from keepflame.core.models import FlameState, KeepStatus, RunOutcome
from keepflame.core.plugins import PluginError
from keepflame.runtime import Runtime, build_runtime
from keepflame.storage.sqlite import StorageError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keepflame")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("run", "run-once", "status", "health"):
        command = subparsers.add_parser(name)
        command.add_argument(
            "--config",
            type=Path,
            default=default_config_path(),
            help="runtime TOML configuration path",
        )
        if name == "run-once":
            command.add_argument("--flame", help="run only the named Flame")
        if name == "status":
            command.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _configured_flames(runtime: Runtime, selected: str | None) -> list[FlameConfig]:
    flames = list(runtime.config.enabled_flames())
    if selected is None:
        if not flames:
            raise ConfigError("no enabled flames are configured")
        return flames
    matching = [flame for flame in flames if flame.id == selected]
    if not matching:
        raise ConfigError(f"enabled flame not found: {selected}")
    return matching


def _outcome_dict(outcome: RunOutcome) -> dict[str, object]:
    result = outcome.record.result
    return {
        "event": "run_once_result",
        "timestamp": outcome.record.finished_at.astimezone(timezone.utc).isoformat(),
        "run_id": outcome.record.run_id,
        "flame_id": outcome.record.flame_id,
        "account_id": outcome.record.account_id,
        "status": result.status.value,
        "message": result.message,
        "metadata": result.metadata,
        "next_action_at": (
            None
            if result.next_action_at is None
            else result.next_action_at.astimezone(timezone.utc).isoformat()
        ),
        "human_action": None if result.human_action is None else result.human_action.to_dict(),
        "reward": None if result.reward is None else result.reward.to_dict(),
    }


def _run_once(runtime: Runtime, selected: str | None) -> int:
    outcomes = [
        runtime.orchestrator.run_flame(flame, notify=False)
        for flame in _configured_flames(runtime, selected)
    ]
    for outcome in outcomes:
        print(
            json.dumps(
                _outcome_dict(outcome),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    statuses = {outcome.record.result.status for outcome in outcomes}
    if KeepStatus.FAILED in statuses:
        return 1
    if KeepStatus.WAITING_HUMAN in statuses:
        return 2
    return 0


def _state_dict(flame: FlameConfig, state: FlameState | None) -> dict[str, object]:
    base: dict[str, object] = {
        "flame_id": flame.id,
        "account_id": flame.account_id,
        "keeper": flame.keeper,
        "enabled": flame.enabled,
    }
    if state is None:
        return {**base, "status": "NOT_RUN"}
    return {
        **base,
        "status": state.status.value,
        "message": state.message,
        "last_checked_at": state.last_checked_at.astimezone(timezone.utc).isoformat(),
        "last_success_at": (
            None
            if state.last_success_at is None
            else state.last_success_at.astimezone(timezone.utc).isoformat()
        ),
        "consecutive_failures": state.consecutive_failures,
        "pending_human_since": (
            None
            if state.pending_human_since is None
            else state.pending_human_since.astimezone(timezone.utc).isoformat()
        ),
        "human_action": None if state.human_action is None else state.human_action.to_dict(),
        "next_action_at": (
            None
            if state.next_action_at is None
            else state.next_action_at.astimezone(timezone.utc).isoformat()
        ),
        "metadata": state.metadata,
    }


def _status(runtime: Runtime, as_json: bool) -> int:
    items = [
        _state_dict(flame, runtime.storage.get_state(flame.id, flame.account_id))
        for flame in runtime.config.flames
    ]
    if as_json:
        print(
            json.dumps(
                {"service_ok": True, "flames": items},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    print(f"{'FLAME':<28} {'ACCOUNT':<18} {'STATUS':<16} NEXT ACTION")
    for item in items:
        print(
            f"{item['flame_id']:<28} {item['account_id']:<18} "
            f"{item['status']:<16} {item.get('next_action_at') or '-'}"
        )
        if item.get("message"):
            print(f"  {item['message']}")
        action = item.get("human_action")
        if isinstance(action, dict):
            print(f"  HUMAN: {action['summary']}")
            print(f"  {action['instructions']}")
            if action.get("url"):
                print(f"  {action['url']}")
    return 0


def _health(runtime: Runtime) -> int:
    runtime.storage.healthcheck()
    print(
        json.dumps(
            {
                "service_ok": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        )
    )
    return 0


def _run_daemon(runtime: Runtime) -> int:
    def stop(_signum, _frame) -> None:
        runtime.service.stop_event.set()

    previous_handlers = {
        signal.SIGINT: signal.signal(signal.SIGINT, stop),
        signal.SIGTERM: signal.signal(signal.SIGTERM, stop),
    }
    runtime.notifier.emit(
        "service_started",
        config=str(runtime.config.source_path),
        enabled_flames=len(runtime.config.enabled_flames()),
    )
    try:
        runtime.service.run_forever()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        runtime.notifier.emit("service_stopped")
    return 0


def _error(message: str, error_type: str) -> int:
    print(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "command_failed",
                "error_type": error_type,
                "message": message,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runtime = build_runtime(args.config)
        if args.command == "run":
            return _run_daemon(runtime)
        if args.command == "run-once":
            return _run_once(runtime, args.flame)
        if args.command == "status":
            return _status(runtime, args.as_json)
        if args.command == "health":
            return _health(runtime)
        raise AssertionError(f"unhandled command: {args.command}")
    except (ConfigError, PluginError, StorageError) as exc:
        return _error(str(exc), type(exc).__name__)
    except Exception as exc:
        return _error("command failed safely", type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
