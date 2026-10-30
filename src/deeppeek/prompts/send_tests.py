"""`deeppeek prompt send_tests` — run the test suite, then pipe the
log plus the specific tests and fixture artifacts that failed.

Pipeline:

    test       deeppeek test -o <log>            (optionally --filter)
    read       <log>.json — the structured test report written by
               `deeppeek test -o`.  No log scraping, no source AST
               parsing: the report already carries per-test status
               and fixture IDs.
    collect    for each failed test file, the test file itself;
               for each per-test failure with a fixture ID, the
               fixtures/<id>.md, expected/<id>/*, and
               preexisting/<id>/* that test operates on
    pipe       tests.log + collected files
    type       short templated prompt

What this command deliberately DOES NOT attach:

  · unrelated test files that passed
  · unrelated fixtures that passed
  · conftest.py / helpers.py
  · any source from src/deeppeek/

The design principle is "the exact minimum for the AI to diagnose
the failure and propose a fix."  If the failure requires seeing
production source, that's `deeppeek prompt refine_deeppeek`'s job —
this one stays narrow on purpose.

Test-report contract
====================
`deeppeek test -o PATH` writes both `<PATH>` (the human-readable
transcript) and `<PATH>.json` (the structured report).  This module
reads the JSON.  Schema is documented in `commands/test.py`.  If the
schema's `version` field is anything but 1, we refuse to proceed —
a schema bump means `send_tests.py` needs a matching update.

Fixture resolution
==================
Each `test_recover.py` test emits a `::DSX_TEST::<json>` line with
its fixture ID derived from the test body.  The `tests` array in the
JSON report is these records.  There is NO fixture-ID inference on
this side; the ID travels with the data.

Test files that don't emit the protocol (all unit-test files other
than `test_recover.py`) get `tests: []` in the report — the failure
is at whole-file granularity, so the file itself is the artifact.

Exit code
=========
  0 — all tests passed; nothing sent
  1 — infrastructure failure (log or report not produced, schema
      mismatch, etc.)
  rc — reflected exit code from the pipe/type stage

Standalone invocation
=====================
`python -m deeppeek.prompts.send_tests [flags]` runs the same
pipeline as `deeppeek prompt send_tests [flags]`, without needing
the `send_tests` subcommand word.  Useful when the installed
deeppeek console-script is stale but the source tree is current, or
when a diagnostic needs to bypass the CLI dispatcher.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_RECOVER_TESTS_DIR = _PROJECT_ROOT / "tests" / "recover"
_FIXTURES_DIR = _RECOVER_TESTS_DIR / "fixtures"
_EXPECTED_DIR = _RECOVER_TESTS_DIR / "expected"
_PREEXISTING_DIR = _RECOVER_TESTS_DIR / "preexisting"

_DEFAULT_LOG_PATH = "/tmp/tests.log"
_REPORT_SCHEMA_VERSION = 1


_DEFAULT_PROMPT_TEMPLATE = """\
My deeppeek test suite reported failures.  The full test log is
attached as `tests.log`.

I am also attaching the failing test file(s).  For integration
failures in `test_recover.py`, I have additionally attached the
fixture markdown, the expected output tree, and the pre-existing
baseline that each failed test operates on (when each exists).
Nothing else is attached — no source from `src/deeppeek/`, no
unrelated fixtures, no unrelated test files.

Please diagnose the failures and propose fixes.  Prefer fixes at
the level of the tool's passes, heuristics, and structural helpers
over patches that special-case one specific fixture.  The goal is a
general-purpose recovery pipeline, not one that passes these
specific tests by narrowing its behavior.

Failures:

{failure_summary}
"""


# ---------------------------------------------------------------------------
# Subprocess helpers — duplicated from the other prompt modules
# ---------------------------------------------------------------------------

def _deeppeek_cmd() -> list[str]:
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _run_cmd(cmd: list[str], *,
             timeout: Optional[float] = None,
             allow_failure: bool = False) -> tuple[int, str, str]:
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        msg = f"`{cmd[0]}` not on PATH"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    except subprocess.TimeoutExpired:
        msg = f"timed out after {timeout}s"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    if proc.returncode != 0:
        tag = "(allowed)" if allow_failure else ""
        print(f"    exit {proc.returncode} {tag}".rstrip(),
              file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Report loading
# ---------------------------------------------------------------------------

def _load_report(report_path: Path) -> dict:
    """Read and validate the test report.

    Raises SystemExit with a clear message on any failure — the
    report is the sole input to this command's collection step, so
    a missing or malformed report is fatal, not degradable.
    """
    if not report_path.is_file():
        print(f"error: test report not produced at {report_path}",
              file=sys.stderr)
        print(f"  is deeppeek up to date?  `deeppeek test -o` "
              f"should write a sibling .json report alongside the "
              f"transcript.  If not, `pipx reinstall deeppeek` (or "
              f"re-`pipx install --editable`).", file=sys.stderr)
        raise SystemExit(1)

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"error: cannot read test report {report_path}: {e}",
              file=sys.stderr)
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        print(f"error: test report {report_path} is not valid JSON: "
              f"{e}", file=sys.stderr)
        raise SystemExit(1)

    version = report.get("version")
    if version != _REPORT_SCHEMA_VERSION:
        print(f"error: test report {report_path} has schema "
              f"version {version!r} (expected "
              f"{_REPORT_SCHEMA_VERSION}).  `send_tests.py` and "
              f"`test.py` are out of sync — reinstall deeppeek.",
              file=sys.stderr)
        raise SystemExit(1)

    return report


# ---------------------------------------------------------------------------
# Artifact collection
# ---------------------------------------------------------------------------

def _collect_failure_files(failed_files: list[dict]) -> list[Path]:
    """Return Paths for every file the failures touch.

    For each failed test file, the test file itself.  For each
    per-test failure with a fixture ID, the fixture `.md`, everything
    under `expected/<id>/*`, and everything under
    `preexisting/<id>/*`.
    """
    out: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        if not p.is_file():
            return
        if p in seen:
            return
        seen.add(p)
        out.append(p)

    # The test files themselves.
    for entry in failed_files:
        _add(_PROJECT_ROOT / entry["path"])

    # Fixture artifacts for per-test failures.
    for entry in failed_files:
        for test in entry.get("tests", []):
            if test.get("status") not in ("FAIL", "ERROR"):
                continue
            fixture_id = test.get("fixture")
            if not fixture_id:
                continue

            _add(_FIXTURES_DIR / f"{fixture_id}.md")

            expected_dir = _EXPECTED_DIR / fixture_id
            if expected_dir.is_dir():
                for p in sorted(expected_dir.rglob("*")):
                    _add(p)

            preexisting_dir = _PREEXISTING_DIR / fixture_id
            if preexisting_dir.is_dir():
                for p in sorted(preexisting_dir.rglob("*")):
                    _add(p)

    return sorted(out, key=lambda p: str(p))


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _format_failure_summary(failed_files: list[dict]) -> str:
    """Human-readable summary of which files failed, and for files
    that emitted per-test records, which fixtures."""
    lines: list[str] = []
    for entry in failed_files:
        path = entry["path"]
        failed_tests = [
            t for t in entry.get("tests", [])
            if t.get("status") in ("FAIL", "ERROR")
        ]
        if failed_tests:
            named = [
                t.get("fixture") or f"<test_{t.get('name')}>"
                for t in failed_tests
            ]
            lines.append(
                f"  · {path} — failed fixtures: "
                + ", ".join(named)
            )
        else:
            exit_code = entry.get("exit_code")
            lines.append(
                f"  · {path} — whole file (exit {exit_code}; "
                f"no per-test breakdown)"
            )
    return "\n".join(lines) if lines else "  · (unknown)"


def _build_prompt(failure_summary: str) -> str:
    return _DEFAULT_PROMPT_TEMPLATE.format(
        failure_summary=failure_summary,
    )


# ---------------------------------------------------------------------------
# Composer emit (duplicated from refine_deeppeek.py per convention)
# ---------------------------------------------------------------------------

def _stage_emit_parallel(prompt, files, args):
    """Open one composer session, fire attach + type back-to-back,
    collect acks."""
    import base64
    import mimetypes
    from ..commands._composer import broadcast_many, composer_session

    if prompt is not None and not prompt.strip():
        print("error: composer text is empty/whitespace-only",
              file=sys.stderr)
        return 1

    event_specs = []

    if files:
        payload_files = []
        for f in files:
            if not f.is_file():
                print(f"  skipped (not a regular file): {f}",
                      file=sys.stderr)
                continue
            data = f.read_bytes()
            mime = (mimetypes.guess_type(f.name)[0]
                    or "application/octet-stream")
            payload_files.append({
                "name": f.name,
                "mime": mime,
                "base64": base64.b64encode(data).decode("ascii"),
            })
            print(f"  {f.name}  ({len(data):,} bytes)",
                  file=sys.stderr)
        if payload_files:
            event_specs.append({
                "tag": "pipe",
                "event": "attach",
                "payload": {
                    "files": payload_files,
                    "opts": {"delay_ms": 0},
                },
                "ack_name": "attach",
            })

    if prompt:
        event_specs.append({
            "tag": "type",
            "event": "type",
            "payload": {"text": prompt},
            "ack_name": "type",
        })

    if not event_specs:
        return 0

    timeout = max(
        getattr(args, "pipe_timeout", 180.0),
        getattr(args, "type_timeout", 300.0),
    )
    with composer_session(port=getattr(args, "port", 8765),
                         timeout=timeout) as server:
        if server is None:
            return 1
        print(f"[parallel] firing {len(event_specs)} event(s):",
              file=sys.stderr)
        for s in event_specs:
            print(f"  {s['event']} (tag={s['tag']})",
                  file=sys.stderr)
        results = broadcast_many(server, event_specs, timeout=timeout)
        rc = 0
        for s in event_specs:
            r = results.get(s["tag"],
                            {"exit_code": 1, "result": {}})
            if r["exit_code"] == 0:
                print(f"  OK - {s['tag']}", file=sys.stderr)
            else:
                reason = (r["result"].get("reason")
                          or r["result"].get("error")
                          or "(no reason)")
                print(f"  FAIL - {s['tag']}: {reason}",
                      file=sys.stderr)
                if rc == 0:
                    rc = r["exit_code"]
        return rc


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    if args.text is not None and not args.text.strip():
        print("error: --text is empty (or whitespace-only).  Omit "
              "--text to use the default prompt.",
              file=sys.stderr)
        return 2

    log_path = (Path(args.log_path) if args.log_path
                else Path(_DEFAULT_LOG_PATH))

    # ---- 1. Run the test suite ------------------------------------
    cmd = _deeppeek_cmd() + ["test", "-o", str(log_path)]
    if args.filter:
        cmd.extend(["--filter", args.filter])
    print(f"[test] running `deeppeek test -o {log_path}`",
          file=sys.stderr)
    rc, _, _ = _run_cmd(cmd, timeout=args.test_timeout,
                        allow_failure=True)

    if not log_path.is_file():
        print(f"error: test log not produced at {log_path}",
              file=sys.stderr)
        print("  (is deeppeek installed editable, and is .venv "
              "present?)", file=sys.stderr)
        return 1

    # ---- 2. Load the structured report ---------------------------
    report_path = log_path.with_suffix(".json")
    report = _load_report(report_path)

    if report.get("exit_code") == 0:
        print("[test] all tests passed; nothing to send",
              file=sys.stderr)
        return 0

    failed_files = [
        f for f in report.get("files", []) if f.get("failed")
    ]

    if not failed_files:
        print(
            f"warning: test runner exited {rc} but no failed test "
            f"files are recorded in the report (setup error?); "
            f"attaching the log only",
            file=sys.stderr,
        )
        failure_summary = (
            "  · (no per-test breakdown available — see tests.log "
            "for the error)"
        )
        related: list[Path] = []
    else:
        related = _collect_failure_files(failed_files)
        failure_summary = _format_failure_summary(failed_files)
        print(
            f"[report] {len(failed_files)} failed test file(s), "
            f"{len(related)} related artifact(s)",
            file=sys.stderr,
        )
        for f in failed_files:
            print(f"  {f['path']}", file=sys.stderr)

    # ---- 3. Build the prompt --------------------------------------
    if args.text is not None:
        prompt = args.text
        print(f"[build] prompt: {len(prompt):,} chars "
              f"(--text override)", file=sys.stderr)
    else:
        prompt = _build_prompt(failure_summary)
        print(f"[build] prompt: {len(prompt):,} chars "
              f"(default template)", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] would attach:", file=sys.stderr)
        print(f"  {log_path}", file=sys.stderr)
        for f in related:
            print(f"  {f}", file=sys.stderr)
        print(f"\n[dry-run] prompt below; not typed.", file=sys.stderr)
        print(prompt)
        return 0

    # ---- 4. Pipe + type -------------------------------------------
    if args.no_pipe:
        files: list[Path] = []
    else:
        files = [log_path] + related

    return _stage_emit_parallel(prompt, files, args)


# ---------------------------------------------------------------------------
# Flag definitions — shared by `register` (CLI) and `main` (standalone)
# ---------------------------------------------------------------------------

def _add_args(p: argparse.ArgumentParser) -> None:
    """Add send_tests's flags to `p`.

    Factored out so both entry points add exactly the same flags in
    exactly the same order.  If a flag is added in one place and not
    the other, the two invocations drift — this helper is the single
    source of truth.
    """
    p.add_argument(
        "--log-path", default=None, metavar="PATH",
        help=f"where to write the raw test log "
             f"(default: {_DEFAULT_LOG_PATH}).  The JSON report "
             f"`deeppeek test` writes is read from `<PATH>.json`.",
    )
    p.add_argument(
        "--filter", default=None, metavar="PATTERN",
        help="only run test files whose basename contains PATTERN "
             "(case-insensitive); forwarded to `deeppeek test "
             "--filter`.  Useful when you already know which file "
             "regressed.",
    )
    p.add_argument(
        "--test-timeout", type=float, default=1800.0,
        help="seconds for `deeppeek test` (default: 1800)",
    )
    p.add_argument(
        "--text", default=None, metavar="STR",
        help="override the default prompt.  STR is typed into the "
             "composer verbatim.  The attachments are still piped "
             "unless --no-pipe is also given.",
    )
    p.add_argument(
        "--no-pipe", action="store_true",
        help="type only; don't attach any files",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="run the tests, print the would-attach list and the "
             "prompt, then stop — no pipe, no type",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--pipe-timeout", type=float, default=180.0,
        help="seconds for the pipe event (default: 180)",
    )
    p.add_argument(
        "--type-timeout", type=float, default=300.0,
        help="seconds for the type event (default: 300)",
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "send_tests",
        help="Run the test suite, then pipe the log plus the tests "
             "and fixtures that failed for AI analysis",
    )
    _add_args(p)
    p.set_defaults(func=run)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """Standalone runner for `python -m deeppeek.prompts.send_tests`.

    Same flags as `deeppeek prompt send_tests`, but without the
    `send_tests` subcommand word.  Useful when the console script on
    PATH is stale (pipx editable symlink vs. a just-edited source
    tree), or when a diagnostic needs to bypass the CLI dispatcher.
    """
    parser = argparse.ArgumentParser(
        prog="python -m deeppeek.prompts.send_tests",
        description="Standalone entry point for `deeppeek prompt "
                    "send_tests`.",
    )
    _add_args(parser)
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
