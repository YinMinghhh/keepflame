"""Tushare daily points Flame plugin (sign-in, browse new data, guess tomorrow)."""

from keepflame.flames.tushare.keeper import TushareKeeper


def create_keeper() -> TushareKeeper:
    return TushareKeeper()


__all__ = ["TushareKeeper", "create_keeper"]
