"""Next-day eligibility for a pending attempt that submitted nothing.

The decision uses the recorded challenge, answer, and claim counts. It does
not know which Flame produced those counts, and it does not grant an archive
certificate.
"""

from __future__ import annotations


def claim_count(metadata: object) -> int | None:
    """Return the recorded claim submissions, or None when the count is absent."""
    if not isinstance(metadata, dict):
        return None
    counts = metadata.get("stage_counts")
    if not isinstance(counts, dict):
        return None
    claim = counts.get("claim")
    if type(claim) is int:
        return claim
    return None


def _unsubmitted(metadata: object, key: str) -> bool:
    """True when this stage was not sent. A missing challenge or answer key is zero.

    A missing claim is handled by claim_count and stays blocked. A bool or other
    non-integer is not evidence of zero submissions.
    """
    if not isinstance(metadata, dict):
        return False
    counts = metadata.get("stage_counts")
    if not isinstance(counts, dict) or key not in counts:
        return True
    value = counts[key]
    return type(value) is int and not isinstance(value, bool) and value == 0


def zero_claim_pending_allows_new_day(action_day: object, state: object, metadata: object, today: object) -> bool:
    """Allow a later business day only when no challenge, answer, or claim was sent.

    The same day stays blocked. A missing claim count stays blocked. A submitted
    challenge, answer, or claim stays blocked. No archive row is required or created.
    """
    return (
        state == "pending"
        and isinstance(action_day, str)
        and isinstance(today, str)
        and action_day < today
        and claim_count(metadata) == 0
        and _unsubmitted(metadata, "challenge")
        and _unsubmitted(metadata, "answer")
    )


def zero_claim_pending_allows_retry(action_day: object, state: object, metadata: object, today: object) -> bool:
    """Like the next-day rule, but the same business day may also retry.

    Only a pending that sent no challenge, answer, or claim qualifies. Any
    submitted write, or a missing claim count, keeps the duplicate guard.
    """
    return (
        state == "pending"
        and isinstance(action_day, str)
        and isinstance(today, str)
        and action_day <= today
        and claim_count(metadata) == 0
        and _unsubmitted(metadata, "challenge")
        and _unsubmitted(metadata, "answer")
    )


# Pauses the core adds for its own safety checks stay until an explicit resume.
_CORE_SAFETY_STOPS = frozenset({"clock_regressed", "completion_receipt_missing_or_invalid"})


def pre_request_failure(metadata: object) -> bool:
    """A Keeper-declared failure before any task request or reservation.

    It carries no stage counts because no guarded request path existed. Any
    recorded count means a request path did exist and the claim rules apply.
    """
    return (isinstance(metadata, dict) and metadata.get("pre_request_failure") is True
            and "stage_counts" not in metadata
            and metadata.get("reason") not in _CORE_SAFETY_STOPS)


def pre_request_failure_allows_new_day(action_day: object, metadata: object, today: object) -> bool:
    """A flame pause left by a pre-request failure expires on a later business day."""
    return (isinstance(action_day, str) and isinstance(today, str) and action_day < today
            and pre_request_failure(metadata))


def execution_paused(storage, keeper: str, account_id: str, flame_id: str, *, now) -> bool:
    """Whether this Flame should be skipped. Storage without the newer check stays strict."""
    checker = getattr(storage, "blocks_execution", None)
    if callable(checker):
        return bool(checker(keeper, account_id, flame_id, now=now))
    return bool(storage.is_paused(keeper, account_id, flame_id))
