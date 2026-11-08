"""Top-level CLI dispatcher.  Subcommand bodies live in commands/<name>.py.

Lazy subcommand loading: only the command module for the subcommand
being run is imported.  `deeppeek type` no longer pays for the
recover, capture, prompt, extension, hijack, or daemon import chains.
Full-parser construction (which imports every command module) is
reserved for `--help`, `--version`, and unknown-subcommand error
paths.
"""
import argparse
import importlib
import os
import sys

from . import __version__


# Subcommand name → module under `deeppeek.commands`.  Insertion order
# matches the original eager import block, so `--help` lists
# subcommands in the same order as before.
_SUBCOMMAND_MODULES = {
    "capture":      "capture",
    "conversation": "conversation",
    "recover":      "recover",
    "restore":      "restore",
    "residual":     "residual",
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

# Subcommands whose in-process code path consumes tree-sitter via
# `recover.ast_anchor`.  `_maybe_reexec_into_ast_venv` imports
# `deeppeek.recover.ast_anchor` unconditionally — a `recover` package
# import on every CLI invocation.  Gating the re-exec on these names
# skips that import for `type` / `enter` / `pipe` / `hijack` /
# `daemon` / `extension` / `diagnose` / `test` / `restore` /
# `residual` / `scripts`, none of which touch AST in-process.
# `prompt` shells out to `deeppeek recover` for its AST-consuming
# subcommands; the child process runs its own check, so the parent
# doesn't need to re-exec.
_AST_DEPENDENT = {"recover", "capture", "conversation", "paths"}

def _load_command(name: str):
    """Import `deeppeek.commands.<name>` and return the module."""
    if name not in _SUBCOMMAND_MODULES:
        raise KeyError(f"unknown subcommand: {name!r}")
    return importlib.import_module(
        f".commands.{_SUBCOMMAND_MODULES[name]}",
        package=__package__,
    )


def _maybe_reexec_into_ast_venv(argv: list[str]) -> None:
    """Re-exec into the repo .venv when tree-sitter is only there.

    (Unchanged body — see the docstring in the original file.  This
    function is a no-op when `DEEPEEK_AST_REEXEC=1` is already set, or
    when AST is available in-process.)
    """
    if os.environ.get("DEEPEEK_AST_REEXEC") == "1":
        return
    try:
        from deeppeek.recover.ast_anchor import (
            ast_status, _find_venv_python, _src_dir,
        )
    except Exception:
        return
    try:
        st = ast_status()
    except Exception:
        return
    if st.get("in_process") or not st.get("via_venv"):
        return
    py = _find_venv_python()
    if py is None:
        return
    env = os.environ.copy()
    env["DEEPEEK_AST_REEXEC"] = "1"
    src = str(_src_dir())
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src + (os.pathsep + prev if prev else "")
    try:
        os.execve(str(py), [str(py), "-m", "deeppeek", *argv], env)
    except OSError as e:
        print(
            f"[cli] AST re-exec into {py} failed: {e}; falling back "
            f"to subprocess-per-query mode (correct but slower)",
            file=sys.stderr,
        )
        return


def _new_root_parser() -> argparse.ArgumentParser:
    """The shared root parser skeleton (no subcommands yet).

    Both the full and targeted builders start from this so `--version`
    and `-v` behave identically whether we're building the full tree
    or a single-subcommand one.
    """
    p = argparse.ArgumentParser(
        prog="deeppeek",
        description="Capture DeepSeek conversations and recover files from them.",
    )
    p.add_argument("--version", action="version",
                   version=f"deeppeek {__version__}")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="verbose diagnostics to stderr")
    return p


def _build_full_parser() -> argparse.ArgumentParser:
    """Full parser — imports every command module.

    Only used for --help, --version, and unknown-subcommand error
    paths, so the eager import cost is paid once per such invocation
    rather than on every run.
    """
    p = _new_root_parser()
    sub = p.add_subparsers(dest="command", required=True)
    for name in _SUBCOMMAND_MODULES:
        _load_command(name).register(sub)
    return p


def _build_targeted_parser(command: str) -> argparse.ArgumentParser:
    """Parser registering only `command`'s subparser.

    Skips importing every other command module.  Argparse still
    accepts `<command> [args...]` as its argv, matches the subcommand
    name, and dispatches through the same `func=run` mechanism the
    full parser uses.
    """
    p = _new_root_parser()
    sub = p.add_subparsers(dest="command", required=True)
    _load_command(command).register(sub)
    return p


def _peek_subcommand(argv: list[str]) -> str | None:
    """Return the first non-flag token in `argv`, or None.

    Returns None for `--help`, `--version`, or an unrecognised
    leading flag, so the caller falls back to the full parser (which
    prints help / version / an error as appropriate).
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

    # ---- scripts is intercepted before argparse (its flags must
    # reach the underlying script unchanged).  Handled first so
    # `deeppeek scripts` doesn't pay for AST re-exec or command
    # module imports either. ----
    verbose = False
    i = 0
    while i < len(argv) and argv[i] in ("-v", "--verbose"):
        verbose = True
        i += 1
    if i < len(argv) and argv[i] == "scripts":
        scripts_cmd = _load_command("scripts")
        return scripts_cmd.dispatch(argv[i + 1:], verbose=verbose)

    subcmd = _peek_subcommand(argv)

    # Only re-exec into the AST venv for subcommands that actually
    # consume tree-sitter in-process.  `_maybe_reexec_into_ast_venv`
    # imports `deeppeek.recover.ast_anchor` before it can decide
    # anything — skipping it for `type` / `enter` / `pipe` / etc. is
    # the difference between "fast path" and "still pays for recover".
    if subcmd is None or subcmd in _AST_DEPENDENT:
        _maybe_reexec_into_ast_venv(argv)

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
