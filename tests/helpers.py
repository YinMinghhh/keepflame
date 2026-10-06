from __future__ import annotations

from collections import deque
from datetime import datetime

from keepflame.capabilities.http import HttpRequest, HttpResponse
from keepflame.core.models import FlameState, KeepResult, RunRecord


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
        self.states: dict[tuple[str, str], FlameState] = {}
        self.runs: list[RunRecord] = []

    def get_state(self, flame_id: str, account_id: str) -> FlameState | None:
        return self.states.get((flame_id, account_id))

    def list_states(self) -> list[FlameState]:
        return list(self.states.values())

    def record(self, run: RunRecord, state: FlameState) -> None:
        self.runs.append(run)
        self.states[(state.flame_id, state.account_id)] = state

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

