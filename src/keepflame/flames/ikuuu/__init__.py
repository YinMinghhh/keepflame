"""ikuuu daily check-in Flame plugin."""

from keepflame.flames.ikuuu.keeper import IkuuuKeeper


def create_keeper() -> IkuuuKeeper:
    return IkuuuKeeper()


__all__ = ["IkuuuKeeper", "create_keeper"]
