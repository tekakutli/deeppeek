"""`deeppeek test_recover` — run the recovery test suite.

Invokes `<root>/.venv/bin/python <root>/tests/recover/test_recover.py`
directly, so the tests run under the developer's own project venv —
not under whatever pipx shim launched `deeppeek` and not under the
system interpreter.  Reproducibility is the point: `.venv` is the
environment the developer controls, pipx's venv is what pipx
manages.

Prerequisites, all checked before the subprocess is spawned:

  · `<root>/.venv` exists
  · `<root>/.venv/bin/python` (or `.venv\\Scripts\\python.exe` on
    Windows) exists
  · `<root>/tests/recover/test_recover.py` exists
  · the venv interpreter can `import markdown_it`
  · the venv interpreter can `import deeppeek`

Each failure mode gets its own message pointing at the fix, rather
than letting the subprocess die with a cryptic ImportError deep in
the test runner.  This mirrors the "Failure mode to recognise"
section of the README, which exists precisely because the missing
dependency does not surface as an ImportError at the CLI level —
it surfaces as eight silent subprocess failures and a
FileNotFoundError at the end.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


# <root>/src/deeppeek/commands/test_recover.py
#   .parent                       → commands
#   .parent.parent                → deeppeek
#   .parent.parent.parent         → src
#   .parent.parent.parent.parent  → <root>
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TEST_FILE = _PROJECT_ROOT / "tests" / "recover" / "test_recover.py"
_VENV_DIR = _PROJECT_ROOT / ".venv"


def _venv_python() -> Path:
    if sys.platform == "win32":
        return _VENV_DIR / "Scripts" / "python.exe"
    return _VENV_DIR / "bin" / "python"


def _setup_hint() -> str:
    return (
        "  fix:\n"
        f"    python3 -m venv {_VENV_DIR}\n"
        f"    {_venv_python()} -m pip install -e \"{_PROJECT_ROOT}\""
    )


def _probe_venv(venv_python: Path) -> tuple[bool, str]:
    """Return (ok, diagnostic).  Two probes, so the error message
    names the missing module rather than a generic import failure."""
    for mod, label in (("markdown_it", "markdown-it-py"),
                       ("deeppeek", "deeppeek")):
        proc = subprocess.run(
            [str(venv_python), "-c", f"import {mod}"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            lines = (proc.stderr or "").strip().splitlines()
            detail = lines[-1] if lines else "(no detail)"
            return False, (
                f"{label} is not importable in {venv_python}\n"
                f"  {detail}\n"
                f"{_setup_hint()}"
            )
    return True, ""


def _run_tests(venv_python: Path, args: argparse.Namespace) -> int:
    cmd = [str(venv_python), str(_TEST_FILE)]
    if args.verbose:
        print(f"$ {' '.join(cmd)}", file=sys.stderr)
    # Stream stdout/stderr: the PASS/FAIL lines and any [debug]
    # messages should reach the user's terminal in real time, and
    # this subcommand is not in a pipeline that needs captured output.
    proc = subprocess.run(cmd)
    return proc.returncode


def run(args: argparse.Namespace) -> int:
    if not _VENV_DIR.is_dir():
        print(
            f"error: {_VENV_DIR} does not exist.\n{_setup_hint()}",
            file=sys.stderr,
        )
        return 2

    venv_python = _venv_python()
    if not venv_python.is_file():
        print(
            f"error: {venv_python} not found.\n{_setup_hint()}",
            file=sys.stderr,
        )
        return 2

    if not _TEST_FILE.is_file():
        print(
            f"error: {_TEST_FILE} not found.\n"
            f"  this subcommand requires a source checkout with the "
            f"tests/ directory present;\n"
            f"  a pipx-installed copy of deeppeek does not carry the "
            f"test suite.",
            file=sys.stderr,
        )
        return 2

    ok, diagnostic = _probe_venv(venv_python)
    if not ok:
        print(f"error: {diagnostic}", file=sys.stderr)
        return 2

    return _run_tests(venv_python, args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "test_recover",
        help="Run tests/recover/test_recover.py under the project's "
             ".venv interpreter",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="print the full test command before running it",
    )
    p.set_defaults(func=run)
