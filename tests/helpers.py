from __future__ import annotations

from collections import deque
from pathlib import Path
from datetime import datetime
from dataclasses import replace

from keepflame.capabilities.http import HttpRequest, HttpResponse
from keepflame.core.models import ActionAttempt, FlameState, KeepResult, PendingNotification, RunRecord


def write_secret(path: Path, text: str) -> Path:
    """Create a secret file with mode 0600 for FileSecretResolver tests."""
    path = Path(path)
    path.write_text(text)
    path.chmod(0o600)
    return path


class FakeHttpClient:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = deque(responses)
        self.requests: list[tuple[HttpRequest, float]] = []

    def send(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse:
        self.requests.append((request, timeout_seconds))
        if not self.responses:
            raise AssertionError("fake HTTP response queue is empty")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


class StaticSecrets:
    def __init__(self, value: str | Exception) -> None:
        self.value = value
        self.references: list[str] = []

    def read_text(self, reference: str) -> str:
        self.references.append(reference)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class MemoryStorage:
    def __init__(self) -> None:
        self.actions = {}
        self.pauses = {}
        self.states: dict[tuple[str, str], FlameState] = {}
        self.runs: list[RunRecord] = []
        self.notifications: dict[tuple[str, str], PendingNotification] = {}

    def get_action(self, keeper, account_id, task):
        return self.actions.get((keeper, account_id, task))

    def reserve_action(self, keeper, account_id, task, day, now, *, validate_time=None):
        if validate_time is not None: validate_time()
        old = self.get_action(keeper, account_id, task)
        if old is not None and (old.state == "pending" or old.day >= day):
            return False
        self.actions[(keeper, account_id, task)] = ActionAttempt("pending", day, now)
        return True

    def confirm_action(self, keeper, account_id, task, now, *, day, validate_time=None):
        if validate_time is not None: validate_time()
        old = self.get_action(keeper, account_id, task)
        if old is not None and old.state == "pending" and old.day == day:
            self.actions[(keeper, account_id, task)] = replace(old, state="confirmed", confirmed_at=now)
            return True
        return False

    def pause(self, keeper, account_id, scope, reason, now):
        self.pauses[(keeper, account_id, scope)] = reason

    def is_paused(self, keeper, account_id, flame_id):
        return (keeper, account_id, "*") in self.pauses or (keeper, account_id, flame_id) in self.pauses

    def resume(self, keeper, account_id, scope):
        self.pauses.pop((keeper, account_id, scope), None)

    def get_state(self, flame_id: str, account_id: str) -> FlameState | None:
        return self.states.get((flame_id, account_id))

    def list_states(self) -> list[FlameState]:
        return list(self.states.values())

    def record(self, run: RunRecord, state: FlameState, notification=None) -> None:
        self.runs.append(run)
        key = (state.flame_id, state.account_id)
        self.states[key] = state
        if notification is None:
            self.notifications.pop(key, None)
        else:
            self.notifications[key] = notification

    def get_notification(self, flame_id, account_id):
        return self.notifications.get((flame_id, account_id))

    def update_notification(self, notification):
        key = (notification.flame_id, notification.account_id)
        current = self.notifications.get(key)
        if current is not None and current.notification_id == notification.notification_id:
            self.notifications[key] = notification

    def acknowledge_notification(self, notification):
        key = (notification.flame_id, notification.account_id)
        current = self.notifications.get(key)
        if current is not None and current.notification_id == notification.notification_id:
            del self.notifications[key]
            if notification.reminder_at is not None:
                self.states[key] = replace(self.states[key], last_reminder_at=notification.reminder_at)

    def healthcheck(self) -> None:
        return None


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def notify(self, event: str, **fields) -> None:
        self.events.append({"event": event, **fields})


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class SequenceKeeper:
    def __init__(self, *results: KeepResult) -> None:
        self.results = deque(results)

    def keep(self, _context) -> KeepResult:
        if not self.results:
            raise AssertionError("fake Keeper result queue is empty")
        return self.results.popleft()


class StaticRegistry:
    def __init__(self, keeper) -> None:
        self.keeper = keeper

    def resolve(self, _name: str):
        return self.keeper


def response(
    status: int,
    body: str,
    *,
    headers: dict[str, str] | None = None,
    url: str = "https://www.joinquant.com/credits/task/getTaskInfo",
) -> HttpResponse:
    return HttpResponse(
        status=status,
        url=url,
        headers={key.casefold(): value for key, value in (headers or {}).items()},
        body=body.encode(),
    )

