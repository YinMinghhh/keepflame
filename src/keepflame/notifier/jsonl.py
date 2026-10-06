"""Restrained JSON Lines lifecycle notifications."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from typing import TextIO

from keepflame.core.models import KeepResult


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


class JsonlNotifier:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()

    def emit(self, event: str, **fields) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()

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
        payload = {
            "timestamp": occurred_at.astimezone(timezone.utc).isoformat(),
            "event": event,
            "notification_id": notification_id,
            "flame_id": flame_id,
            "account_id": account_id,
            "status": result.status.value,
            "message": result.message,
            "metadata": result.metadata,
            "next_action_at": _timestamp(result.next_action_at),
            "human_action": (
                None if result.human_action is None else result.human_action.to_dict()
            ),
            "reward": None if result.reward is None else result.reward.to_dict(),
        }
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()

