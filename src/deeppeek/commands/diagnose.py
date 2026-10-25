"""`deeppeek diagnose` — environment sanity check."""
import argparse
import shutil
import sys


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("diagnose", help="Print environment / extension / LLM status")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    print(f"python: {sys.version.split()[0]}  ({sys.executable})")
    try:
        import markdown_it  # noqa: F401
        print("markdown-it-py: ok")
    except ImportError:
        print("markdown-it-py: MISSING — `pip install markdown-it-py`")

    try:
        from ..extension.paths import source_dir, zen_root
        from ..extension.pack import extension_version, extension_id
        src = source_dir()
        print(f"extension assets: {src}")
        try:
            print(f"  version: {extension_version(src)}")
            print(f"  id:      {extension_id(src)}")
        except SystemExit as e:
            print(f"  manifest: {e}")
        print(f"zen root: {zen_root()} ({'exists' if zen_root().exists() else 'absent'})")
    except Exception as e:
        print(f"extension: {e}")

    # Status is a best-effort report.  `resolve_profile()` raises
    # `SystemExit` when there are multiple Zen profiles and none was
    # named via `--profile` / `$ZEN_PROFILE` — that is correct
    # behaviour for `extension status` (refuse to guess), but it
    # must not make `diagnose` look like a failure.  Catch it,
    # explain it, keep going.
    try:
        from ..extension.status import print_status
        print("\n--- installed extension status ---")
        print_status()
    except SystemExit as e:
        print(f"  status unavailable: {e}")
        print("  (use `deeppeek extension status --profile <name>` to check)")

    try:
        from ..recover.ast_anchor import ast_status
        st = ast_status()
        if st["in_process"]:
            print("AST anchoring: ok (in-process)")
        elif st["via_venv"]:
            print(f"AST anchoring: ok (via repo .venv subprocess: "
                  f"{st['venv_path']})")
        else:
            print("AST anchoring: unavailable")
            print("  AST is optional; text heuristics still work.")
            print("  To enable it, install in the pipx venv:")
            print("    pipx inject deeppeek tree-sitter "
                  "tree-sitter-javascript tree-sitter-python")
            print("  Or install in the repo .venv (deeppeek will "
                  "load it at runtime):")
            print("    .venv/bin/pip install -e '.[ast]'")
    except Exception as e:
        print(f"AST anchoring: probe failed ({e})")

    if shutil.which("zen") or shutil.which("zen-browser"):
        print("zen binary: ok")
    else:
        print("zen binary: not on PATH (may still be installed)")

    return 0
