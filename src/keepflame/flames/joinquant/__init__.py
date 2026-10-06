"""JoinQuant Flame plugin."""

from keepflame.flames.joinquant.keeper import JoinQuantKeeper


def create_keeper() -> JoinQuantKeeper:
    return JoinQuantKeeper()


__all__ = ["JoinQuantKeeper", "create_keeper"]

