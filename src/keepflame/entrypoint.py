"""Invoke this installation without requiring the ``keepflame`` script to be on PATH."""

from __future__ import annotations

import shlex
import sys


def module_command() -> str:
    """Absolute interpreter plus ``-m keepflame``.

    A user shell can lack the console script even while the LaunchAgent runs
    this same interpreter. The two failures are not the same event.
    """
    return f"{shlex.quote(sys.executable)} -m keepflame"
