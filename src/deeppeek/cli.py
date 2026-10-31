"""Top-level CLI dispatcher.  Subcommand bodies live in commands/<name>.py."""
import argparse
import os
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


def _maybe_reexec_into_ast_venv(argv: list[str]) -> None:
    """Re-exec into the repo .venv when tree-sitter is only there.

    The pre-fix pipeline ran in whatever interpreter invoked it. If
    tree-sitter lived only in `<repo>/.venv`, every AST consumer that
    called `_get_parser()` directly — not just the ones that used the
    `try_ast_anchor` subprocess fallback — silently failed. That
    produced the "scope X matched but replacement produced no
    change" false-negative class in the 2026-10-30 run.

    The invariant: if AST is available via venv but not in-process,
    re-exec once so every consumer sees a real parser. Subprocess
    fallbacks per-call site are a worse solution (each call site has
    to be updated, latency compounds, and diagnostics diverge).

    FIX (2026-11-06): the `except OSError` branch was silent. When
    re-exec failed the pipeline ran in-process with
    `ast_status()["in_process"] == false` and every scope lookup
    misrouted — the exact failure shape of the 2026-10-30 run
    (`scope_ast.find_scope` returning None for scopes that plainly
    exist). The subprocess fallbacks added in `scope_ast.py` and
    `validate_output.py` make re-exec failure a *slower* outcome
    rather than a *wrong* one, so the diagnostic says so.
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
        # Re-exec failed (venv Python missing deeppeek on PYTHONPATH,
        # binary vanished mid-run, permission denied, ...). Fall
        # through to running in-process. The subprocess-per-query
        # fallbacks in scope_ast and validate_output mean the run
        # still produces correct output; it just pays a per-query
        # subprocess cost. Say so rather than silently continuing.
        print(
            f"[cli] AST re-exec into {py} failed: {e}; falling back "
            f"to subprocess-per-query mode (correct but slower)",
            file=sys.stderr,
        )
        return


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
    "scripts": scripts_cmd.run,
    "hijack": hijack_cmd.run,
}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

    # Re-exec into the repo .venv before any AST-dependent subcommand
    # runs, if that is the only interpreter with tree-sitter.
    _maybe_reexec_into_ast_venv(argv)

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
