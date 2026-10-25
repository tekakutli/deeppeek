"""`deeppeek prompt new` — seed a new prompt about the current directory.

Three steps, in order:

    dump     `dump.py -r -o <ws>/project_dump_<cwd>.md` in cwd
    pipe     `deeppeek pipe <dump>`  — the dump as a file chip
    type     `deeppeek type "<text>"` — the prefill

From a fresh terminal, `cd` into a project and run:

    deeppeek prompt new

The composer is left with the project attached as a file chip and
the text "this is my project, I want you to" in the textarea,
waiting for you to finish the sentence and press Enter.

Helpers are intentionally duplicated from `evolve.py` to keep each
prompt self-contained — extract to a shared module when a third
prompt shows up.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DUMP_PY_SCRIPT = _PROJECT_ROOT / "scripts" / "dumps" / "dump.py"

DEFAULT_PROMPT = "this is my project, I want you to"


# ---------------------------------------------------------------------------
# Subprocess helpers (duplicated from evolve.py — see module docstring)
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
        print(f"    exit {proc.returncode} {tag}".rstrip(), file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _dump_to_path(directory: Path, out: Path, timeout: float,
                  dump_py: Optional[Path] = None) -> None:
    """Run `dump.py -r -o <out>` in `directory` and verify the file."""
    script = dump_py or _DUMP_PY_SCRIPT
    if not directory.is_dir():
        raise SystemExit(f"error: dump target is not a directory: {directory}")
    if not script.exists():
        raise SystemExit(
            f"error: dump script not found at {script}\n"
            f"  (pass --dump-py to point at a different script)"
        )
    out.parent.mkdir(parents=True, exist_ok=True)

    python_bin = sys.executable
    cmd = [python_bin, str(script), "-r", "-o", str(out)]
    print(f"    $ (cd {directory} && "
          f"{python_bin} {script} -r -o {out})", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, cwd=str(directory),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"error: dump timed out after {timeout:.0f}s")

    if proc.returncode != 0:
        print(f"    warning: exit {proc.returncode}", file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    for line in (proc.stdout or "").splitlines()[:2]:
        if line.strip():
            print(f"    {line.strip()}", file=sys.stderr)

    if not out.exists() or not out.read_text(encoding="utf-8").strip():
        raise SystemExit(
            f"error: dump.py did not produce {out}\n"
            f"  stdout: {proc.stdout[:200]!r}\n"
            f"  stderr: {proc.stderr[:200]!r}"
        )
    print(f"    wrote {out} ({out.stat().st_size:,} bytes)",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _stage_pipe(dump_path: Path, args: argparse.Namespace) -> int:
    prefix = _deeppeek_cmd()
    print(f"[pipe] attaching {dump_path} "
          f"({dump_path.stat().st_size:,} bytes)", file=sys.stderr)
    rc, _, _ = _run_cmd(
        prefix + ["pipe", str(dump_path)],
        timeout=args.pipe_timeout,
        allow_failure=False,
    )
    return rc


def _stage_type(text: str, args: argparse.Namespace) -> int:
    fd, path = tempfile.mkstemp(
        prefix="deeppeek_new_", suffix=".md",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[type] {len(text):,} chars via `deeppeek type`",
              file=sys.stderr)
        prefix = _deeppeek_cmd()
        rc, _, _ = _run_cmd(
            prefix + ["type", path],
            timeout=args.type_timeout,
            allow_failure=False,
        )
        return rc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass



def _stage_emit_parallel(prompt, files, args):
    """Open one composer session, fire attach + type back-to-back,
    collect acks.  Replaces the sequential pipe + type subprocess
    calls, roughly halving the wall-time cost of the emit stage.

    `files` may be empty/None to skip the pipe event; `prompt` may be
    None to skip the type event.  If both are absent, this is a no-op
    (returns 0).  Empty-string `prompt` (but not None) is an error,
    matching `commands/type.py`'s rejection of empty composer text.
    """
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
    cwd = Path.cwd()
    if args.workspace is None:
        ws = Path(tempfile.mkdtemp(prefix="deeppeek_new_"))
    else:
        ws = args.workspace.resolve()
        ws.mkdir(parents=True, exist_ok=True)

    print(f"[new] project: {cwd}", file=sys.stderr)
    print(f"[new] workspace: {ws}", file=sys.stderr)
    print(f"  (kept after run for inspection; rm -rf {ws} when done)",
          file=sys.stderr)

    dump_path = ws / f"project_dump_{cwd.name}.md"
    print(f"[dump] dump.py -r in {cwd}", file=sys.stderr)
    _dump_to_path(cwd, dump_path, args.dump_py_timeout,
                  dump_py=args.dump_py)

    if args.dry_run:
        print("\n[dry-run] dump written; not piping, not typing.",
              file=sys.stderr)
        print(f"  would pipe: {dump_path}", file=sys.stderr)
        print(f"  would type: {args.text!r}", file=sys.stderr)
        return 0

    files = [] if args.no_pipe else [dump_path]
    return _stage_emit_parallel(args.text, files, args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "new",
        help="Dump the current directory, pipe it, and prefill the composer",
    )

    p.add_argument(
        "--text", default=DEFAULT_PROMPT,
        help=f"text to type into the composer "
             f"(default: {DEFAULT_PROMPT!r})",
    )
    p.add_argument(
        "--workspace", type=Path, default=None,
        help="directory for the dump.  Default: a fresh temp dir in /tmp.  "
             "Point this at a stable location to keep the dump around "
             "for inspection.",
    )
    p.add_argument(
        "--no-pipe", action="store_true",
        help="skip the pipe step; only type the text",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="dump only; print what would be piped and typed, then stop",
    )
    p.add_argument(
        "--dump-py", type=Path, default=None,
        help=f"override the dump.py script path "
             f"(default: {_DUMP_PY_SCRIPT}).  Must support `-r -o PATH`.",
    )
    p.add_argument(
        "--dump-py-timeout", type=float, default=300.0,
        help="seconds for `dump.py -r` (default: 300)",
    )
    p.add_argument(
        "--type-timeout", type=float, default=300.0,
        help="seconds for `deeppeek type` (default: 300)",
    )
    p.add_argument(
        "--pipe-timeout", type=float, default=180.0,
        help="seconds for `deeppeek pipe` (default: 180)",
    )

    p.set_defaults(func=run)
