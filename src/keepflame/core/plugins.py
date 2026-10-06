"""Keeper discovery through Python package entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib.metadata import entry_points

from keepflame.core.protocols import Keeper

KEEPER_ENTRYPOINT_GROUP = "keepflame.keepers"
KeeperFactory = Callable[[], Keeper]


class PluginError(RuntimeError):
    """Raised when a configured Keeper cannot be discovered or constructed."""


class EntryPointKeeperRegistry:
    def __init__(self, factories: Mapping[str, KeeperFactory] | None = None) -> None:
        self._factories = dict(factories or {})
        self._factory_cache: dict[str, KeeperFactory] = {}

    def resolve(self, name: str) -> Keeper:
        factory = self._factory_cache.get(name) or self._factories.get(name)
        if factory is None:
            matches = list(entry_points(group=KEEPER_ENTRYPOINT_GROUP, name=name))
            if not matches:
                raise PluginError(f"keeper plugin not found: {name}")
            if len(matches) > 1:
                raise PluginError(f"multiple keeper plugins found for: {name}")
            loaded = matches[0].load()
            if not callable(loaded):
                raise PluginError(f"keeper entry point is not callable: {name}")
            factory = loaded
        self._factory_cache[name] = factory

        try:
            keeper = factory()
        except Exception as exc:
            raise PluginError(f"keeper plugin failed to initialize: {name}") from exc
        if not callable(getattr(keeper, "keep", None)):
            raise PluginError(f"keeper plugin has no keep method: {name}")
        return keeper
