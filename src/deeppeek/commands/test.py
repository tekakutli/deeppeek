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
               verbose: bool,
               output: Path | None = None) -> int:
    """Run each test file under `venv_python`.

    `output` (when set) is opened for writing at the start of the
    run.  Every line the runner emits — its own `=== file ===`
    headers, per-file verbose path lines, and every line of
    subprocess stdout and stderr — is written to the file AND
    echoed to the terminal in real time.  The transcript is a
    durable copy of everything the user saw.

    Without `output`, behavior is byte-identical to the previous
    implementation: subprocess inherits file descriptors, so
    subprocess stdout/stderr stay on their original terminal
    streams.

    Opening `output` is checked before any test runs.  If it fails
    (permissions, path is a directory, parent uncreatable), returns
    exit code 2 with a clear error and runs nothing.
    """
    failed: list[Path] = []

    log = None
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            log = output.open("w", encoding="utf-8")
        except OSError as e:
            print(f"error: cannot open output file {output}: {e}",
                  file=sys.stderr)
            return 2

    def runner_msg(msg: str) -> None:
        print(msg, file=sys.stderr)
        if log is not None:
            log.write(msg + "\n")
            log.flush()

    def sub_line(line: str) -> None:
        sys.stdout.write(line)
        sys.stdout.flush()
        if log is not None:
            log.write(line)
            log.flush()

    try:
        for f in files:
            rel = f.relative_to(_PROJECT_ROOT)
            runner_msg(f"\n=== {rel} ===")
            if verbose:
                runner_msg(f"    {venv_python} {f}")

            if log is None:
                # Preserve original behavior: subprocess inherits
                # file descriptors, terminal streams untouched.
                proc = subprocess.run([str(venv_python), str(f)])
                if proc.returncode != 0:
                    failed.append(rel)
                continue

            # With -o: tee subprocess output to stdout AND file.
            # stderr is merged into stdout so the transcript has
            # every line in a single interleaved stream, and the
            # file is not missing subprocess warnings that went to
            # fd 2.
            try:
                proc = subprocess.Popen(
                    [str(venv_python), str(f)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                runner_msg(f"    (spawn failed: {venv_python} "
                           f"not found)")
                failed.append(rel)
                continue

            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    sub_line(line)
            finally:
                proc.wait()

            if proc.returncode != 0:
                failed.append(rel)

        runner_msg("")
        if failed:
            runner_msg(f"{len(failed)}/{len(files)} test file(s) failed:")
            for rel in failed:
                runner_msg(f"  {rel}")
            return 1
        runner_msg(f"All {len(files)} test file(s) passed")
        return 0
    finally:
        if log is not None:
            log.close()
            print(f"\nTranscript written to {output}", file=sys.stderr)


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

    return _run_tests(venv_python, files, args.verbose, args.output)


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
    p.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="write the full test transcript (runner messages plus "
             "every line of subprocess stdout and stderr) to PATH. "
             "The transcript is also shown on the terminal in real "
             "time, so PATH is a durable copy of everything you saw. "
             "Existing files at PATH are overwritten. Parent "
             "directories are created if missing.",
    )
    p.set_defaults(func=run)
