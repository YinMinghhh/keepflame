"""Bounded outbox delivery shared by the scheduler and ``notify retry``.

Five failures clear ``retry_at``. Nothing in this module contacts a channel.
``rearm_exhausted`` only makes an exhausted row eligible for another budget.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from keepflame.core.models import PendingNotification

DELIVERY_ATTEMPT_LIMIT = 5
_TOKEN = r"[a-z][a-z0-9_]{0,31}"
_TOKEN_RE = re.compile(rf"^{_TOKEN}$")
_CATEGORY = re.compile(rf"^(?:{_TOKEN}|{_TOKEN}:{_TOKEN}(?:,{_TOKEN}:{_TOKEN})*)$")


def retry_at_after_attempt(attempts: int, now: datetime) -> datetime | None:
    """Next eligible time after this failed attempt, or None when the budget is spent."""
    if attempts >= DELIVERY_ATTEMPT_LIMIT:
        return None
    return now + timedelta(seconds=60 * 2 ** (attempts - 1))


def delivery_error_record(exc: BaseException) -> tuple[str, str]:
    """Exception type plus a safe category token. Never returns exception text."""
    raw = getattr(exc, "category", None)
    if isinstance(raw, str) and _CATEGORY.fullmatch(raw):
        return type(exc).__name__, raw
    return type(exc).__name__, "other"


def channel_failure_category(failures: list[tuple[str, BaseException]]) -> str:
    """``email:other,slack:timeout`` style summary. Drops anything that is not a token."""
    parts: list[str] = []
    for name, exc in failures:
        channel = name if _TOKEN_RE.fullmatch(name) else "channel"
        raw = getattr(exc, "category", None)
        token = raw if isinstance(raw, str) and _TOKEN_RE.fullmatch(raw) else "other"
        parts.append(f"{channel}:{token}")
    category = ",".join(parts)
    if _CATEGORY.fullmatch(category):
        return category
    return "other"


def rearm_exhausted(pending: PendingNotification, *, now: datetime) -> PendingNotification | None:
    """Open another bounded budget for an exhausted row, keeping its identity.

    Returns None when the row still has a retry time. Attempts go back to zero.
    The notification id, event, result, and last error stay, so this is not a
    new outbox event and run history is not rewritten.
    """
    if pending.retry_at is not None:
        return None
    return replace(pending, attempts=0, retry_at=now.astimezone(timezone.utc))
