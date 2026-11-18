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


_SUBCOMMAND_MODULES = {
    "conversation": "conversation",
    "pipe":         "pipe",
    "type":         "type",
    "enter":        "enter",
    "hijack":       "hijack",
    "hijack_turn":  "hijack_turn",
    "prompt":       "prompt",
    "extension":    "extension",
    "diagnose":     "diagnose",
    "test":         "test",
    "scripts":      "scripts",
    "daemon":       "daemon",
    "paths":        "paths",
    "answer":       "answer",
    "editor":       "editor",
}


def _load_command(name: str):
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
    p = _new_root_parser()
    sub = p.add_subparsers(dest="command", required=True)
    for name in _SUBCOMMAND_MODULES:
        _load_command(name).register(sub)
    return p


def _build_targeted_parser(command: str, argv=None) -> argparse.ArgumentParser:
    """Parser registering only `command`'s subparser.

    For `editor`, the parser is built with the `--script` value
    peeked from `argv` so the target script can install its own
    flags via `register_flags(parser)` before argparse runs.  This
    is how `--paths_absolute` and any future script-specific flag
    gets recognised without being globally registered.
    """
    p = _new_root_parser()
    sub = p.add_subparsers(dest="command", required=True)
    mod = _load_command(command)
    if command == "editor":
        script_name = _peek_flag_value(
            argv or [], "--script", "passthrough")
        mod.register(sub, script_name=script_name)
    else:
        mod.register(sub)
    return p


def _peek_subcommand(argv: list[str]) -> str | None:
    for tok in argv:
        if tok in ("-v", "--verbose"):
            continue
        if tok.startswith("-"):
            return None
        return tok
    return None


def _peek_flag_value(argv, flag, default=None):
    """Pre-parse peek: return the value argparse would assign to
    `--flag`, or `default` if absent.

    Handles `--flag value` and `--flag=value`.  Last occurrence
    wins, matching argparse's `store` action.  Stops at `--` so a
    positional argument that literally contains the flag string is
    not misread.  Values starting with `-` are ignored (argparse
    will reject them; we don't pre-parse a phantom script name).
    """
    seen = None
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            break
        if tok == flag:
            if i + 1 < n:
                cand = argv[i + 1]
                if not cand.startswith("-") or cand == "-":
                    seen = cand
                i += 2
                continue
        elif tok.startswith(flag + "="):
            seen = tok[len(flag) + 1:]
        i += 1
    return seen if seen is not None else default


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

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
        parser = _build_targeted_parser(subcmd, argv)
    else:
        parser = _build_full_parser()

    args = parser.parse_args(argv)
    try:
        return _load_command(args.command).run(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
