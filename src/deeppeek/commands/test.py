"""`deeppeek test` — run the project's test suite.

Discovers every `test_*.py` under `<root>/tests/` (recursively) and
runs each one under the project's `.venv` interpreter.  Each test
file is expected to expose `main() -> int` and exit 0 on success,
non-zero on failure — the shape used by `tests/recover/test_recover.py`
and `tests/recover/test_dedup.py`.

Why the venv (and not the interpreter currently running `deeppeek`):
pipx installs deeppeek into an isolated venv that doesn't carry the
test suite's dev dependencies.  The project's own `.venv` does.  The
CLI's job here is to *launch* the suite under the developer's own
interpreter, not to interpret it.

Prerequisites, all checked before any test file is spawned:

  · `<root>/.venv` exists
  · `<root>/.venv/bin/python` (or `.venv\\Scripts\\python.exe` on
    Windows) exists
  · `<root>/tests/` contains at least one `test_*.py`
  · the venv interpreter can `import markdown_it` and `import deeppeek`

Each failure mode gets its own message pointing at the fix.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TESTS_DIR = _PROJECT_ROOT / "tests"
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


def _discover_test_files() -> list[Path]:
    if not _TESTS_DIR.is_dir():
        return []
    return sorted(_TESTS_DIR.rglob("test_*.py"))


def _probe_venv(venv_python: Path) -> tuple[bool, str]:
    """Return (ok, diagnostic).  Two probes so the error names the
    missing module rather than a generic import failure."""
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


def _run_tests(venv_python: Path, files: list[Path],
               verbose: bool) -> int:
    failed: list[Path] = []
    for f in files:
        rel = f.relative_to(_PROJECT_ROOT)
        print(f"\n=== {rel} ===", file=sys.stderr)
        if verbose:
            print(f"    {venv_python} {f}", file=sys.stderr)
        proc = subprocess.run([str(venv_python), str(f)])
        if proc.returncode != 0:
            failed.append(rel)

    print(file=sys.stderr)
    if failed:
        print(f"{len(failed)}/{len(files)} test file(s) failed:",
              file=sys.stderr)
        for rel in failed:
            print(f"  {rel}", file=sys.stderr)
        return 1
    print(f"All {len(files)} test file(s) passed", file=sys.stderr)
    return 0


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

    files = _discover_test_files()
    if not files:
        print(
            f"error: no test_*.py files under {_TESTS_DIR}.\n"
            f"  this subcommand requires a source checkout with the "
            f"tests/ directory;\n"
            f"  a pipx-installed copy of deeppeek does not carry the "
            f"test suite.",
            file=sys.stderr,
        )
        return 2

    ok, diagnostic = _probe_venv(venv_python)
    if not ok:
        print(f"error: {diagnostic}", file=sys.stderr)
        return 2

    if args.filter:
        needle = args.filter.lower()
        files = [f for f in files if needle in f.name.lower()]
        if not files:
            print(
                f"error: no test file matched filter {args.filter!r}",
                file=sys.stderr,
            )
            return 2

    return _run_tests(venv_python, files, args.verbose)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "test",
        help="Run the project's test suite (from <root>/tests/) "
             "under the project's .venv interpreter",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="print each test file's full path before running it",
    )
    p.add_argument(
        "--filter", metavar="PATTERN", default=None,
        help="only run test files whose basename contains PATTERN "
             "(case-insensitive)",
    )
    p.set_defaults(func=run)
