"""`deeppeek prompt send_tests` — bundle failing tests for review.

Reads the structured JSON report that `deeppeek test -o PATH`
writes to `PATH.json`, then bundles every artifact a debugging
session needs into a **single** merged markdown file:

  · the test-runner transcript (the `.txt` from `-o PATH`);
  · every failing test file (once, in a "Test files" group);
  · per-failing-fixture groups, each bundling:
      – the fixture `.md`
      – the `expected/<fixture>/` tree (if present)
      – the `preexisting/<fixture>/` tree (if present).

The merge is done by `scripts/merge_md.py` with one `--group` per
bundle, so the reader sees clean boundaries:

    #  <log file>                                    (group: "test log")
    #  GROUP: test_recover.py                        (test file only)
    #  GROUP: 01_reprint_later (from test_recover.py)
    #    fixtures/01_reprint_later.md
    #    expected/01_reprint_later/bar.py
    #    expected/01_reprint_later/foo.py

Instead of piping N+12 files (log + test + fixture + expected×k +
preexisting×m, per failing test) on N+12 separate attach events, the
whole debugging context lands on ONE file chip — same shape as
`deeppeek prompt refine_deeppeek`.

FIXTURE CONVENTION
==================
The bundler looks for the standard fixture layout:

    <test_file_dir>/fixtures/<fixture>.md
    <test_file_dir>/expected/<fixture>/*
    <test_file_dir>/preexisting/<fixture>/*

For `tests/recover/test_recover.py::01_reprint_later` this resolves to:

    tests/recover/fixtures/01_reprint_later.md
    tests/recover/expected/01_reprint_later/{foo.py,bar.py}
    tests/recover/preexisting/01_reprint_later/*  (if present)

Whole-file failures (report entries with `tests: []`) get a group
with just the test file — nothing else to look for.  Fixtures with
no `expected/` or `preexisting/` directory just omit those parts.

REPORT SOURCE
=============
`--report` and `--log` are two spellings of the same thing:

  · `--log PATH` — pass the `.txt` file you gave to
    `deeppeek test -o PATH`; we look for `PATH.json` beside it.
  · `--report PATH` — pass `PATH.json` directly, or the `.txt`
    path (we'll add `.json` for you).

Exactly one must be given.

DIFFERENCES FROM `refine_deeppeek`
==================================
`refine_deeppeek` runs `recover` first, then merges the aftermath
into one chip.  `send_tests` runs nothing — the report is already on
disk from a previous `deeppeek test -o PATH`.  It only reads,
bundles, pipes, types.

The merge shape (`--group`-per-bundle) is what this module shares
with `refine_deeppeek`: one chip, one prompt, one connection.

PERSISTING THE MERGED FILE
==========================
By default the merged file lands in a fresh temp workspace under
/tmp, which may be cleaned up between sessions.  `--save PATH`
copies the merged file out to a stable location:

    send_tests --log run.txt --save /tmp/dumps/
       → /tmp/dumps/send_tests_merged.md

    send_tests --log run.txt --save notes/tests.md
       → notes/tests.md  (parents created)

`--save` is orthogonal to `--no-pipe`: saving persists the file,
piping attaches it to the chat, typing fills the composer; any
combination of those three is valid.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MERGE_MD_SCRIPT = _PROJECT_ROOT / "scripts" / "merge_md.py"


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------
#
# The prompt is short on purpose: the merged file carries the actual
# evidence.  The template's job is (a) name what the model is looking
# at, (b) tell it where the groups are, (c) state the invariant about
# not silently dropping fixes.

DEFAULT_PROMPT_TEMPLATE = """\
The deeppeek test suite just failed.  I've attached a single merged
markdown file ({merged_name}) that bundles every artifact you need to
diagnose and fix it:

  1. The test-runner transcript ({log_name}) — the .txt file that
     `deeppeek test -o` wrote.  Shows exactly what failed and why.
  2. A group called "Test files" with each failing test file
     verbatim.
  3. Per-failing-fixture groups, one per failure:
       · the fixture markdown (`fixtures/<name>.md`),
       · the expected output tree (`expected/<name>/*`), and
       · the pre-existing baseline (`preexisting/<name>/*`)
     bundled together so each failure is self-contained.

Failing:
{failing_summary}

"""


# ---------------------------------------------------------------------------
# Report reading
# ---------------------------------------------------------------------------

def _report_path_from_args(args: argparse.Namespace) -> Path:
    """Locate the JSON report.

    Priority:
      1. --report FILE (explicit; if suffix != ".json", add it back
         because `deeppeek test -o x.txt` writes `x.json`).
      2. --log FILE   (the .txt transcript path; derive .json).
    """
    if args.report is not None:
        p = args.report
        if p.suffix != ".json":
            p = p.with_suffix(".json")
        return p
    if args.log is not None:
        return args.log.with_suffix(".json")
    raise SystemExit(
        "error: pass --report PATH.json or --log PATH.txt"
    )


def _read_report(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"error: report not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"error: cannot read {path}: {e}")


# ---------------------------------------------------------------------------
# Failure extraction
# ---------------------------------------------------------------------------

def _collect_failing_tests(report: dict) -> list[dict]:
    """Flatten the report into `{test_file, test_name, fixture,
    status, error}` records.  Handles both per-test failures (with
    the `::DSX_TEST::` protocol) and whole-file failures (report
    entry with `tests: []`)."""
    out: list[dict] = []
    for f in report.get("files", []):
        if not f.get("failed"):
            continue
        tests = f.get("tests") or []
        if not tests:
            out.append({
                "test_file": f["path"],
                "test_name": None,
                "fixture": None,
                "status": "ERROR",
                "error": "whole test file failed; no per-test records",
            })
            continue
        for t in tests:
            if t.get("status") in ("FAIL", "ERROR"):
                out.append({
                    "test_file": f["path"],
                    "test_name": t.get("name"),
                    "fixture": t.get("fixture"),
                    "status": t.get("status"),
                    "error": t.get("error"),
                })
    return out


# ---------------------------------------------------------------------------
# Fixture artifact resolution
# ---------------------------------------------------------------------------

def _fixture_files(test_file: Path, fixture: str) -> list[Path]:
    """Locate the fixture .md, expected tree, and pre-existing tree
    for one (test_file, fixture) pair.

    Convention:
      <test_dir>/fixtures/<fixture>.md
      <test_dir>/expected/<fixture>/...
      <test_dir>/preexisting/<fixture>/...
    """
    test_dir = test_file.parent
    out: list[Path] = []

    fix_md = test_dir / "fixtures" / f"{fixture}.md"
    if fix_md.is_file():
        out.append(fix_md)

    expected = test_dir / "expected" / fixture
    if expected.is_dir():
        for p in sorted(expected.rglob("*")):
            if p.is_file():
                out.append(p)

    preexisting = test_dir / "preexisting" / fixture
    if preexisting.is_dir():
        for p in sorted(preexisting.rglob("*")):
            if p.is_file():
                out.append(p)

    return out


# ---------------------------------------------------------------------------
# Group manifest
# ---------------------------------------------------------------------------

def _build_groups(report: dict,
                  project_root: Path,
                  log_path: Optional[Path]) -> list[dict]:
    """Build the ordered list of `{name, files}` groups to hand to
    `merge_md.py --group`.  Order:

      1. "test log"                    (if a transcript path is known)
      2. "Test files"                  (distinct failing test files)
      3. "<fixture> (from <test>)"     per failing fixture
    """
    failing = _collect_failing_tests(report)
    groups: list[dict] = []

    # 1. Test log — the primary diagnostic.
    if log_path is not None and log_path.is_file():
        groups.append({
            "name": "test log",
            "files": [log_path],
        })

    # 2. Distinct failing test files.
    test_files: list[Path] = []
    seen_tf: set[Path] = set()
    for t in failing:
        p = project_root / t["test_file"]
        if p.is_file() and p not in seen_tf:
            test_files.append(p)
            seen_tf.add(p)
    if test_files:
        groups.append({
            "name": "Test files",
            "files": test_files,
        })

    # 3. Per-fixture groups.  Fixture name alone plus its owning test
    # file (basename) so the reader can tell which test asserted it.
    seen_fx: set[tuple[str, str]] = set()
    for t in failing:
        fixture = t["fixture"]
        if not fixture:
            continue
        test_file = project_root / t["test_file"]
        key = (t["test_file"], fixture)
        if key in seen_fx:
            continue
        seen_fx.add(key)

        fx_files = _fixture_files(test_file, fixture)
        if not fx_files:
            # Fixture named in the report but no artifacts found on
            # disk — surface as an empty-note group so the operator
            # sees the omission.
            continue
        groups.append({
            "name": (f"{fixture} "
                     f"(from {Path(t['test_file']).name})"),
            "files": fx_files,
        })

    return groups


# ---------------------------------------------------------------------------
# Subprocess + merge
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str], *,
             timeout: Optional[float] = None
             ) -> tuple[int, str, str]:
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise SystemExit(f"error: `{cmd[0]}` not on PATH")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"error: timed out after {timeout}s")
    if proc.returncode != 0:
        print(f"    exit {proc.returncode}", file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _stage_merge(groups: list[dict],
                 ws: Path,
                 args: argparse.Namespace) -> Path:
    """Invoke `merge_md.py` with one `--group` per bundle.

    A group with no files (shouldn't happen, but defensively) is
    skipped.  Returns the merged file path.
    """
    script = _MERGE_MD_SCRIPT
    if not script.is_file():
        raise SystemExit(
            f"error: merge_md.py not found at {script}"
        )

    out = ws / "send_tests_merged.md"
    cmd: list[str] = [
        sys.executable, str(script),
        "-o", str(out),
        "-t", "send_tests — merged failing-test artifacts",
    ]

    for g in groups:
        if not g["files"]:
            continue
        cmd.append("--group")
        cmd.append(g["name"])
        for f in g["files"]:
            cmd.append(str(f))

    rc, _stdout, stderr = _run_cmd(cmd, timeout=args.merge_timeout)
    if rc != 0:
        raise SystemExit(
            f"error: merge_md.py exited {rc}\n{stderr}"
        )
    if not out.is_file():
        raise SystemExit(
            f"error: merge_md.py reported success but did not "
            f"write {out}"
        )
    return out


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _build_prompt(report: dict,
                  log_path: Optional[Path],
                  merged_path: Path) -> str:
    failing = _collect_failing_tests(report)
    if not failing:
        failing_summary = "  (none — the report shows no failures)"
    else:
        lines: list[str] = []
        for t in failing:
            status = t["status"]
            test_file = t["test_file"]
            fixture = t["fixture"]
            name = t["test_name"]
            if fixture:
                lines.append(
                    f"  · {test_file} :: {fixture} — {status}"
                )
            elif name:
                lines.append(
                    f"  · {test_file} :: test_{name} — {status}"
                )
            else:
                lines.append(f"  · {test_file} — {status}")
        failing_summary = "\n".join(lines)

    log_name = log_path.name if log_path is not None else "(not provided)"
    return DEFAULT_PROMPT_TEMPLATE.format(
        merged_name=merged_path.name,
        log_name=log_name,
        failing_summary=failing_summary,
    )


# ---------------------------------------------------------------------------
# Pipe + type in parallel (one composer session)
# ---------------------------------------------------------------------------

def _stage_emit_parallel(prompt: str,
                         files: list[Path],
                         args: argparse.Namespace) -> int:
    """One composer session, one pipe event + one type event fired
    back-to-back.  Same shape as `refine_deeppeek`."""
    from ..commands._composer import broadcast_many, composer_session

    if prompt is not None and not prompt.strip():
        print("error: composer text is empty/whitespace-only",
              file=sys.stderr)
        return 1

    event_specs: list[dict] = []

    if files:
        payload_files: list[dict] = []
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
    report_path = _report_path_from_args(args)
    log_path = args.log if args.log is not None else None

    report = _read_report(report_path)
    failing = _collect_failing_tests(report)
    if not failing:
        print("error: report shows no failing tests",
              file=sys.stderr)
        return 1

    print(f"[report] {report_path} — {len(failing)} failure(s)",
          file=sys.stderr)
    for t in failing:
        fx = f" :: {t['fixture']}" if t["fixture"] else ""
        print(f"  {t['test_file']}{fx}  [{t['status']}]",
              file=sys.stderr)

    # Workspace
    if args.workspace is None:
        ws = Path(tempfile.mkdtemp(prefix="deeppeek_send_tests_"))
    else:
        ws = args.workspace.resolve()
        ws.mkdir(parents=True, exist_ok=True)
    print(f"[workspace] {ws}", file=sys.stderr)
    print(f"  (kept after run for inspection; rm -rf {ws} when done)",
          file=sys.stderr)

    # Build groups
    project_root = args.project_root.resolve()
    groups = _build_groups(report, project_root, log_path)

    total_files = sum(len(g["files"]) for g in groups)
    print(f"[groups] {len(groups)} group(s), "
          f"{total_files} file(s)", file=sys.stderr)
    for g in groups:
        print(f"  · {g['name']}: {len(g['files'])} file(s)",
              file=sys.stderr)

    merged_placeholder = ws / "send_tests_merged.md"

    if args.dry_run:
        print("\n[dry-run] would merge:", file=sys.stderr)
        for g in groups:
            print(f"  --group {g['name']!r}", file=sys.stderr)
            for f in g["files"]:
                print(f"    {f}", file=sys.stderr)
        if args.save is not None:
            print(
                f"\n[dry-run] would also save merged file to: "
                f"{args.save}",
                file=sys.stderr,
            )
        prompt = _build_prompt(report, log_path, merged_placeholder)
        print(f"\n[dry-run] prompt ({len(prompt):,} chars) below; "
              f"not typed.", file=sys.stderr)
        print(prompt)
        return 0

    if not groups:
        print("error: no artifacts to merge — the report names "
              "failures but no test file or fixture files were "
              "found on disk", file=sys.stderr)
        return 1

    # Merge
    merged_path = _stage_merge(groups, ws, args)
    print(f"[merge] {merged_path} "
          f"({merged_path.stat().st_size:,} bytes)",
          file=sys.stderr)

    # Optional persistent copy.
    #
    # The workspace under /tmp may be cleaned up between sessions
    # (by the OS, by a user `rm -rf`, or by the caller's own
    # cleanup).  `--save PATH` writes a copy out to a stable
    # location so the merged file survives.  Independent of
    # `--no-pipe`: saving is orthogonal to whether we also
    # attach-and-type.
    if args.save is not None:
        save_target = args.save
        if save_target.exists() and save_target.is_dir():
            save_target = save_target / merged_path.name
        elif str(save_target).endswith(("/", "\\")):
            try:
                save_target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(
                    f"error: cannot create directory {save_target}: "
                    f"{e}", file=sys.stderr,
                )
                return 1
            save_target = save_target / merged_path.name
        try:
            save_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(merged_path, save_target)
        except OSError as e:
            print(
                f"error: cannot save to {save_target}: {e}",
                file=sys.stderr,
            )
            return 1
        print(
            f"[save] also wrote {save_target} "
            f"({save_target.stat().st_size:,} bytes)",
            file=sys.stderr,
        )

    # Build prompt
    prompt = _build_prompt(report, log_path, merged_path)
    print(f"[build] prompt: {len(prompt):,} chars",
          file=sys.stderr)

    files = [] if args.no_pipe else [merged_path]
    return _stage_emit_parallel(prompt, files, args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "send_tests",
        help="Bundle failing-test artifacts into a single merged "
             "file via merge_md.py, attach that one file, and type a "
             "diagnostic prompt (reads the JSON report from "
             "`deeppeek test -o PATH`)",
    )

    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--report", type=Path, default=None, metavar="PATH",
        help="path to the JSON report from `deeppeek test -o X`. "
             "The -o flag writes both X (transcript) and X.json "
             "(structured report); pass either here and we'll find "
             "the .json file.",
    )
    src.add_argument(
        "--log", type=Path, default=None, metavar="PATH",
        help="shorthand for --report: the transcript path from "
             "`deeppeek test -o PATH`. We look for PATH.json beside "
             "it and use that.  The transcript itself is also "
             "attached to the merged file as the top-level context "
             "group.",
    )

    p.add_argument(
        "--project-root", type=Path, default=_PROJECT_ROOT,
        help=f"project root, for resolving test paths "
             f"(default: {_PROJECT_ROOT})",
    )
    p.add_argument(
        "--workspace", type=Path, default=None,
        help="directory for the merged file.  Default: a fresh temp "
             "dir in /tmp.  Point this at a stable location to keep "
             "the artifacts around for inspection.",
    )
    p.add_argument(
        "--save", type=Path, default=None, metavar="PATH",
        help="After merging, also write a copy of the merged file "
             "to PATH so it survives workspace cleanup.  "
             "If PATH is an existing directory, the file lands "
             "inside it as `send_tests_merged.md`; a trailing "
             "slash creates the directory.  Otherwise PATH is used "
             "as the full target filename (parent directories are "
             "created as needed).  An existing file at PATH is "
             "overwritten.  `--dry-run` prints the would-save "
             "target without writing.",
    )
    p.add_argument(
        "--merge-timeout", type=float, default=120.0,
        help="seconds for `merge_md.py` (default: 120)",
    )
    p.add_argument(
        "--no-pipe", action="store_true",
        help="type the prompt but don't attach the merged file",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="print the would-merge groups and the prompt to stdout; "
             "do not merge, pipe, or type",
    )
    p.add_argument(
        "--pipe-timeout", type=float, default=180.0,
        help="seconds for the pipe event (default: 180)",
    )
    p.add_argument(
        "--type-timeout", type=float, default=300.0,
        help="seconds for the type event (default: 300)",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="port for the local extension server (default: 8765)",
    )

    p.set_defaults(func=run)
