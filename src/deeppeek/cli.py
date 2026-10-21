"""Top-level CLI dispatcher.  Subcommand bodies live in commands/<name>.py."""
import argparse
import sys

from . import __version__
from .commands import (
    capture as capture_cmd,
    recover as recover_cmd,
    extension as extension_cmd,
    diagnose as diagnose_cmd,
    pipe as pipe_cmd,
)


def _build_root_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deeppeek",
        description="Capture DeepSeek conversations and recover files from them.",
    )
    p.add_argument("--version", action="version", version=f"deeppeek {__version__}")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="verbose diagnostics to stderr")
    sub = p.add_subparsers(dest="command", required=True)
    capture_cmd.register(sub)
    recover_cmd.register(sub)
    pipe_cmd.register(sub)
    extension_cmd.register(sub)
    diagnose_cmd.register(sub)
    return p


_DISPATCH = {
    "capture": capture_cmd.run,
    "recover": recover_cmd.run,
    "pipe": pipe_cmd.run,
    "extension": extension_cmd.run,
    "diagnose": diagnose_cmd.run,
}


def main(argv=None) -> int:
    args = _build_root_parser().parse_args(argv)
    try:
        return _DISPATCH[args.command](args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
