"""Top-level CLI dispatcher.  Subcommand bodies live in commands/<name>.py."""
import argparse
import sys

from . import __version__
from .commands import (
    capture as capture_cmd,
    conversation as conversation_cmd,
    recover as recover_cmd,
    restore as restore_cmd,
    residual as residual_cmd,
    extension as extension_cmd,
    diagnose as diagnose_cmd,
    pipe as pipe_cmd,
    type as type_cmd,
    enter as enter_cmd,
    prompt as prompt_cmd,
    test as test_cmd,
    scripts as scripts_cmd,
    hijack as hijack_cmd,
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
    conversation_cmd.register(sub)
    recover_cmd.register(sub)
    restore_cmd.register(sub)
    residual_cmd.register(sub)
    pipe_cmd.register(sub)
    type_cmd.register(sub)
    enter_cmd.register(sub)
    hijack_cmd.register(sub)
    prompt_cmd.register(sub)
    extension_cmd.register(sub)
    diagnose_cmd.register(sub)
    test_cmd.register(sub)
    # `scripts` registers for --help listing only; the real dispatch
    # bypasses argparse (see main() below).
    scripts_cmd.register(sub)
    return p


_DISPATCH = {
    "capture": capture_cmd.run,
    "conversation": conversation_cmd.run,
    "recover": recover_cmd.run,
    "restore": restore_cmd.run,
    "residual": residual_cmd.run,
    "pipe": pipe_cmd.run,
    "type": type_cmd.run,
    "enter": enter_cmd.run,
    "prompt": prompt_cmd.run,
    "extension": extension_cmd.run,
    "diagnose": diagnose_cmd.run,
    "test": test_cmd.run,
    # Unreachable in normal operation — main() intercepts `scripts`
    # before parse_args.  Entry present only so a future refactor
    # that forgets the intercept fails with a clear message rather
    # than a KeyError.
    "scripts": scripts_cmd.run,
    "hijack": hijack_cmd.run,
}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

    # ------------------------------------------------------------------
    # `scripts` is dispatched before argparse: everything after the
    # subcommand name (including flags like --merge, -a, --help) must
    # reach the target script verbatim, which argparse would otherwise
    # consume or reject.  See commands/scripts.py.
    #
    # We tolerate a leading `-v` / `--verbose` (matching the global
    # flag's position), so `deeppeek -v scripts dumps dumps_evolve`
    # also works.  Anything after the script name is the script's own
    # concern.
    # ------------------------------------------------------------------
    verbose = False
    i = 0
    while i < len(argv) and argv[i] in ("-v", "--verbose"):
        verbose = True
        i += 1
    if i < len(argv) and argv[i] == "scripts":
        return scripts_cmd.dispatch(argv[i + 1:], verbose=verbose)

    args = _build_root_parser().parse_args(argv)
    try:
        return _DISPATCH[args.command](args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
