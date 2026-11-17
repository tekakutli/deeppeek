"""Top-level CLI dispatcher.  Subcommand bodies live in commands/<name>.py.

Lazy subcommand loading: only the command module for the subcommand
being run is imported.  Full-parser construction (which imports every
command module) is reserved for `--help`, `--version`, and
unknown-subcommand error paths.
"""
import argparse
import importlib
import sys

from . import __version__


# Subcommand name → module under `deeppeek.commands`.  Insertion order
# is the `--help` listing order.
_SUBCOMMAND_MODULES = {
    "conversation": "conversation",
    "pipe":         "pipe",
    "type":         "type",
    "enter":        "enter",
    "hijack":       "hijack",
    "prompt":       "prompt",
    "extension":    "extension",
    "diagnose":     "diagnose",
    "test":         "test",
    "scripts":      "scripts",
    "daemon":       "daemon",
    "paths":        "paths",
}


def _load_command(name: str):
    """Import `deeppeek.commands.<name>` and return the module."""
    if name not in _SUBCOMMAND_MODULES:
        raise KeyError(f"unknown subcommand: {name!r}")
    return importlib.import_module(
        f".commands.{_SUBCOMMAND_MODULES[name]}",
        package=__package__,
    )


def _new_root_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deeppeek",
        description="Capture DeepSeek conversations into markdown and "
                    "drive the chat composer from the CLI.",
    )
    p.add_argument("--version", action="version",
                   version=f"deeppeek {__version__}")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="verbose diagnostics to stderr")
    return p


def _build_full_parser() -> argparse.ArgumentParser:
    """Full parser — imports every command module.

    Only used for --help, --version, and unknown-subcommand error
    paths, so the eager import cost is paid once per such
    invocation rather than on every run.
    """
    p = _new_root_parser()
    sub = p.add_subparsers(dest="command", required=True)
    for name in _SUBCOMMAND_MODULES:
        _load_command(name).register(sub)
    return p


def _build_targeted_parser(command: str) -> argparse.ArgumentParser:
    """Parser registering only `command`'s subparser."""
    p = _new_root_parser()
    sub = p.add_subparsers(dest="command", required=True)
    _load_command(command).register(sub)
    return p


def _peek_subcommand(argv: list[str]) -> str | None:
    """Return the first non-flag token in `argv`, or None.

    Returns None for `--help`, `--version`, or an unrecognised
    leading flag, so the caller falls back to the full parser.
    """
    for tok in argv:
        if tok in ("-v", "--verbose"):
            continue
        if tok.startswith("-"):
            return None
        return tok
    return None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

    # scripts is intercepted before argparse: its flags must reach
    # the underlying script unchanged.
    verbose = False
    i = 0
    while i < len(argv) and argv[i] in ("-v", "--verbose"):
        verbose = True
        i += 1
    if i < len(argv) and argv[i] == "scripts":
        scripts_cmd = _load_command("scripts")
        return scripts_cmd.dispatch(argv[i + 1:], verbose=verbose)

    subcmd = _peek_subcommand(argv)

    if subcmd is not None and subcmd in _SUBCOMMAND_MODULES:
        parser = _build_targeted_parser(subcmd)
    else:
        parser = _build_full_parser()

    args = parser.parse_args(argv)
    try:
        return _load_command(args.command).run(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
