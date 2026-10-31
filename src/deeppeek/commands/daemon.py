"""``deeppeek daemon`` — thin shim into the daemon package.

Mirrors the shape every other command module uses (``register`` +
``run``), so ``cli.py``'s import block, parser wiring, and dispatch
map all work without a special case.
"""
from ..daemon.cli import register as _register, run as _run


def register(sub) -> None:
    _register(sub)


def run(args) -> int:
    return _run(args)