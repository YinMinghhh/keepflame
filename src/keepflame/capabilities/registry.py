"""A small typed capability resolver."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar, cast

T = TypeVar("T")


class CapabilityError(RuntimeError):
    pass


class CapabilityRegistry:
    def __init__(self, capabilities: Mapping[str, object]) -> None:
        self._capabilities = dict(capabilities)

    def require(self, name: str, expected_type: type[T] | None = None) -> T:
        try:
            capability = self._capabilities[name]
        except KeyError as exc:
            raise CapabilityError(f"required capability is unavailable: {name}") from exc
        if expected_type is not None and not isinstance(capability, expected_type):
            raise CapabilityError(f"capability has unexpected type: {name}")
        return cast(T, capability)

