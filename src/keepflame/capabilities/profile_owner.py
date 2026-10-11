"""One process owns an enrolled browser profile. A second start does not launch Chrome."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from keepflame.capabilities.browser import ProfileNotAuthorized, validate_profile


class ProfileInUse(RuntimeError):
    """The enrolled profile already has a KeepFlame owner."""


class ProfileOwner:
    def __init__(self) -> None:
        self._handles: list[int] = []

    def acquire(self, profiles: list[tuple[str, str, str]]) -> None:
        acquired: list[int] = []
        try:
            for profile, account_id, _origin in profiles:
                handle = _lock(profile)
                acquired.append(handle)
                validate_profile(profile, account_id)
        except Exception:
            _release(acquired)
            raise
        self._handles.extend(acquired)

    def release(self) -> None:
        _release(self._handles)
        self._handles.clear()


def browser_profiles(config, registry) -> list[tuple[str, str, str]]:
    """Enabled browser flames, one entry per profile directory."""
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for flame in config.enabled_flames():
        profile = flame.settings.get("browser_profile_dir")
        if not isinstance(profile, str) or not profile or profile in seen:
            continue
        keeper = registry.resolve(flame.keeper)
        origin = getattr(keeper, "browser_origin", None)
        if not isinstance(origin, str) or not origin:
            continue
        seen.add(profile)
        found.append((profile, flame.account_id, origin))
    return found


def prepare_visible_owner(runtime, profiles: list[tuple[str, str, str]]) -> int:
    """Launch one visible browser for each owned profile. Reuse an open context."""
    pool = runtime.owned_browser
    if pool is None:
        return 0
    for profile, account_id, origin in profiles:
        pool.open_visible(profile_dir=profile, account_id=account_id, origin=origin)
    return pool.launch_count


def _lock(profile: str) -> int:
    path = Path(profile)
    if not path.is_absolute() or not path.is_dir():
        raise ProfileNotAuthorized("a dedicated absolute browser profile is required")
    target = path / ".keepflame-owner.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        handle = os.open(target, flags, 0o600)
    except OSError as exc:
        raise ProfileNotAuthorized("browser profile owner lock is not available") from exc
    try:
        info = os.fstat(handle)
        if info.st_uid != os.getuid():
            raise ProfileNotAuthorized("browser profile owner lock must belong to this user")
        os.fchmod(handle, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(handle)
        raise ProfileInUse("profile is already owned") from exc
    except Exception:
        os.close(handle)
        raise
    return handle


def _release(handles: list[int]) -> None:
    while handles:
        handle = handles.pop()
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)
