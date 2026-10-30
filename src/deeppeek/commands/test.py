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

Structured output (2026-11-05)
==============================
When `-o/--output PATH` is given, this command writes TWO files:

  · `<PATH>` — the human-readable transcript: runner messages plus
    every line of subprocess stdout and stderr, tee'd to the
    terminal and to disk in real time.

  · `<PATH>.json` — a structured report of the run.  Schema
    (versioned):

        {
          "version": 1,
          "log_path": "<PATH>" or null,
          "python": "<venv interpreter>",
          "started_at": "<ISO-8601 UTC>",
          "finished_at": "<ISO-8601 UTC>",
          "exit_code": 0 | 1 | -1,
          "abnormal_exit": true,      # only on -1 (uncaught error)
          "files": [
            {
              "path": "tests/recover/test_recover.py",
              "exit_code": 1,
              "failed": true,
              "tests": [
                {
                  "name": "01",
                  "fixture": "01_reprint_later",
                  "status": "PASS" | "FAIL" | "ERROR",
                  "error": null | "<message>"
                },
                …
              ]
            },
            {
              "path": "tests/recover/test_dedup.py",
              "exit_code": 1,
              "failed": true,
              "tests": []       # no per-test protocol emitted
            }
          ]
        }

The `tests` array is populated by parsing `::DSX_TEST::<json>` lines
from each test file's stdout.  Test files that don't emit that
protocol (all the unit-test files besides `test_recover.py`) get
`tests: []` — the whole file is a failure at whole-file granularity.

The report exists so downstream consumers (notably
`deeppeek prompt send_tests`) do NOT have to parse the human-
readable transcript.  The log format is freeform; the JSON report
is a stable, versioned contract.  If the report format ever changes
in a breaking way, bump `version`.

Report is only written when `-o` is given.  Without `-o`, subprocess
output streams directly to the terminal and no structured data is
collected.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TESTS_DIR = _PROJECT_ROOT / "tests"
_VENV_DIR = _PROJECT_ROOT / ".venv"

_PROTOCOL_PREFIX = "::DSX_TEST::"


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


def _parse_protocol_lines(lines: list[str]) -> list[dict]:
    """Extract the `::DSX_TEST::<json>` records from `lines`.

    Returns only well-formed dict records.  A malformed payload
    (bad JSON, non-dict) is silently skipped — that shape can only
    come from a buggy test file, and aborting the report because one
    line didn't parse would hide every other test's result.
    """
    records: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(_PROTOCOL_PREFIX):
            continue
        payload = stripped[len(_PROTOCOL_PREFIX):].strip()
        try:
            record = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


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

    When `output` is set, a structured report is also written to
    `<output>.json` (see module docstring).  Without `output`, no
    report is written — subprocess output streams directly and is
    not captured.

    Opening `output` is checked before any test runs.  If it fails
    (permissions, path is a directory, parent uncreatable), returns
    exit code 2 with a clear error and runs nothing.
    """
    failed: list[Path] = []

    report: dict = {
        "version": 1,
        "log_path": str(output) if output is not None else None,
        "python": str(venv_python),
        "started_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "finished_at": None,
        "exit_code": None,
        "files": [],
    }

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

            file_entry: dict = {
                "path": str(rel),
                "exit_code": None,
                "failed": None,
                "tests": [],
            }

            if log is None:
                # Preserve original behavior: subprocess inherits
                # file descriptors, terminal streams untouched.
                proc = subprocess.run([str(venv_python), str(f)])
                file_entry["exit_code"] = proc.returncode
                file_entry["failed"] = proc.returncode != 0
                if proc.returncode != 0:
                    failed.append(rel)
                report["files"].append(file_entry)
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
                file_entry["exit_code"] = -1
                file_entry["failed"] = True
                failed.append(rel)
                report["files"].append(file_entry)
                continue

            assert proc.stdout is not None
            captured_lines: list[str] = []
            try:
                for line in proc.stdout:
                    captured_lines.append(line)
                    sub_line(line)
            finally:
                proc.wait()

            file_entry["exit_code"] = proc.returncode
            file_entry["failed"] = proc.returncode != 0
            file_entry["tests"] = _parse_protocol_lines(captured_lines)
            if proc.returncode != 0:
                failed.append(rel)
            report["files"].append(file_entry)

        runner_msg("")
        if failed:
            runner_msg(f"{len(failed)}/{len(files)} test file(s) failed:")
            for rel in failed:
                runner_msg(f"  {rel}")
            exit_code = 1
        else:
            runner_msg(f"All {len(files)} test file(s) passed")
            exit_code = 0

        report["finished_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        report["exit_code"] = exit_code
        return exit_code
    finally:
        if log is not None:
            log.close()
            print(f"\nTranscript written to {output}", file=sys.stderr)

            # If the try block raised (uncaught exception), the
            # finished_at / exit_code / abnormal_exit fields above
            # were never populated.  Populate them now so the
            # partial report is self-describing.
            if report["finished_at"] is None:
                report["finished_at"] = (
                    datetime.now(timezone.utc).isoformat(
                        timespec="seconds"))
                report["exit_code"] = -1
                report["abnormal_exit"] = True

            report_path = output.with_suffix(".json")
            try:
                report_path.write_text(
                    json.dumps(report, indent=2),
                    encoding="utf-8",
                )
                print(f"Test report written to {report_path}",
                      file=sys.stderr)
            except OSError as e:
                print(f"warning: could not write test report "
                      f"to {report_path}: {e}", file=sys.stderr)


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
             "A structured JSON report of the run is additionally "
             "written to `<PATH>.json` (same directory, .json "
             "suffix); this is what `deeppeek prompt send_tests` "
             "reads.  Existing files at PATH are overwritten. "
             "Parent directories are created if missing.",
    )
    p.set_defaults(func=run)
