"""One page of official JoinQuant credit history. A missing line is not an amount."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

HISTORY_URL = "https://www.joinquant.com/user/account/creditsDesc?page=1"
ZONE = ZoneInfo("Asia/Shanghai")
TASK_REMARKS = {"checkin": "签到成功", "reading": "浏览文章"}
_UNPROVEN = {"proven": False, "amount": None}
_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def credit_evidence(before: object, after: object, *, task: str, started_at: datetime) -> dict:
    """Record one new positive line, or an unproven amount.

    The line must be absent from the earlier page, belong to this task, start
    at or after the run time truncated to seconds, and be the only reason the
    page total increased by one. Code 20000, a missing page, and an unchanged
    page contribute no amount.
    """
    earlier = _parse(before)
    later = _parse(after)
    remark = TASK_REMARKS.get(task)
    if earlier is None or later is None or remark is None:
        return dict(_UNPROVEN)
    earliest = started_at.astimezone(ZONE).replace(microsecond=0)
    known = {entry["fingerprint"] for entry in earlier["entries"]}
    fresh = [
        entry for entry in later["entries"]
        if entry["fingerprint"] not in known
        and entry["remark"] == remark
        and entry["amount"] > 0
        and entry["at"] >= earliest
    ]
    if len(fresh) != 1 or later["total"] != earlier["total"] + 1:
        return dict(_UNPROVEN)
    line = fresh[0]
    return {"proven": True, "at": line["at"].isoformat(), "item": line["remark"], "amount": line["amount"]}


def fetch_credit_history(page) -> dict | None:
    """Read one history page through the browser's own request channel."""
    request = getattr(getattr(page, "context", None), "request", None)
    getter = getattr(request, "get", None)
    if not callable(getter):
        return None
    response = getter(
        HISTORY_URL,
        headers={
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
        },
        timeout=10_000,
        max_redirects=0,
        max_retries=0,
    )
    try:
        if getattr(response, "status", None) != 200:
            return None
        payload = response.json()
    finally:
        dispose = getattr(response, "dispose", None)
        if callable(dispose):
            dispose()
    return payload if isinstance(payload, dict) else None


def _parse(payload: object) -> dict | None:
    if not isinstance(payload, dict) or payload.get("code") != "00000":
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        return None
    records = data["list"]
    total = _whole(data.get("total"), 100_000_000)
    if total is None or len(records) > 50 or total < len(records):
        return None
    entries = []
    for record in records:
        entry = _entry(record)
        if entry is None:
            return None
        entries.append(entry)
    return {"total": total, "entries": entries}


def _entry(record: object) -> dict | None:
    if not isinstance(record, dict):
        return None
    value = record.get("addTime")
    remark = record.get("remark")
    direction = record.get("ruleType")
    if not isinstance(value, str) or not _CLOCK.fullmatch(value):
        return None
    if not isinstance(remark, str) or not remark or len(remark) > 1024:
        return None
    if direction not in ("+", "-"):
        return None
    amount = _whole(record.get("amount"), 1_000_000)
    if amount is None:
        return None
    signed = amount if direction == "+" else -amount
    try:
        at = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZONE)
    except ValueError:
        return None
    key = json.dumps([value, remark, signed], ensure_ascii=False, separators=(",", ":"))
    return {"at": at, "remark": remark, "amount": signed, "fingerprint": sha256(key.encode()).hexdigest()}


def _whole(value: object, maximum: int) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,8}", value):
        value = int(value)
    if type(value) is int and 0 <= value <= maximum:
        return value
    return None
