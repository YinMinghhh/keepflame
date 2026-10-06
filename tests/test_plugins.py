from keepflame.core.plugins import EntryPointKeeperRegistry
from keepflame.flames.joinquant import JoinQuantKeeper


def test_joinquant_is_discovered_through_entry_point() -> None:
    registry = EntryPointKeeperRegistry()
    first = registry.resolve("joinquant")
    second = registry.resolve("joinquant")
    assert isinstance(first, JoinQuantKeeper)
    assert isinstance(second, JoinQuantKeeper)
    assert first is not second
