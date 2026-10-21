"""Verbosity-aware stderr helpers."""
import sys

_VERBOSE = False


def set_verbose(v: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(v)


def info(msg: str) -> None:
    print(msg, file=sys.stderr)


def debug(msg: str) -> None:
    if _VERBOSE:
        print(f"[debug] {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)
