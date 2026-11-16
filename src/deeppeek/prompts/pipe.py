"""`deeppeek prompt pipe` — attach files, using split_send as backend.

Takes a list of files (like `deeppeek pipe`).  For each file, decides
whether to split-and-send via `deeppeek prompt split_send`, or to
fall back to `deeppeek pipe` on the original file unchanged.

DECISION PER FILE
=================
We run `dumps_split.py <file> -o <tmpdir>` up front and inspect the
result.  Three outcomes:

  1. `dumps_split.py` exits non-zero — the file has no `FILE i/N:`
     blocks (e.g. a plain conversation markdown).  dumps_split.py
     prints `error: no FILE blocks parsed from ...` and writes
     nothing.  We pipe the original file.

  2. `dumps_split.py` exits 0 and produces exactly one part — the
     file DID have FILE blocks but they all fit inside one chunk
     (`<stem>_part1of1.md`).  Piping the part would add extra
     `PART 1/1` headers for zero benefit.  We pipe the original
     file instead.

  3. `dumps_split.py` exits 0 and produces N ≥ 2 parts.  We write
     `progress.json` so `split_send` can run in `--workspace` mode
     (using the parts we already created — no re-splitting) and
     invoke `deeppeek prompt split_send --workspace <tmpdir>` for
     the actual send.

The point: `deeppeek prompt pipe a.md b.md c.md` does the right
thing for every file regardless of size.  Real dumps get split and
sent part-by-part; small files and plain markdown get attached as
a single chip.

USAGE
=====

    deeppeek prompt pipe /tmp/dump.md
       → split into N parts and send via split_send (N >= 2)

    deeppeek prompt pipe /tmp/conversation.md
       → pipe as one chip (no FILE blocks to split)

    deeppeek prompt pipe /tmp/tiny_dump.md
       → pipe as one chip (fits in a single part)

    deeppeek prompt pipe a.md b.md c.md
       → each file routed independently

    deeppeek prompt pipe --dry-run a.md b.md
       → print the routing decision per file; do not pipe or split

Exit codes:
    0  all files routed and delivered successfully
    1  at least one file failed to pipe or to send
    3  argument error (missing file)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SPLIT_SCRIPT = _PROJECT_ROOT / "scripts" / "dumps" / "dumps_split.py"
PROGRESS_FILE_NAME = "progress.json"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _deeppeek_cmd() -> list[str]:
    """Prefer the installed `deeppeek` binary; fall back to
    `python -m deeppeek` so the wrapper works from source checkouts
    too.  Same shape used elsewhere in the codebase."""
    import shutil
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_progress_json(
    ws: Path, input_path: Path, parts: list[Path],
) -> None:
    """Write the minimal progress.json that split_send's --workspace
    mode expects.  Fields match what `prompts/split_send.py` writes
    at the start of a fresh run; `completed` is empty because
    nothing has been sent yet."""
    data = {
        "input_sha256": _sha256_of(input_path),
        "input_path": str(input_path),
        "total": len(parts),
        "completed": [],
        "current": None,
        "status": "starting",
        "started_at": int(time.time()),
    }
    (ws / PROGRESS_FILE_NAME).write_text(
        json.dumps(data, indent=2), encoding="utf-8",
    )


def _list_parts(ws: Path) -> list[Path]:
    """Sorted list of `<stem>_part<i>of<N>.md` files in `ws`.

    Matches the filenames `dumps_split.py` produces.  A plain
    `glob` is enough — no ambiguity with other content since
    dumps_split.py only writes these."""
    return sorted(ws.glob("*_part*of*.md"))


# ---------------------------------------------------------------------------
# Per-file routing
# ---------------------------------------------------------------------------

def _try_split(input_path: Path, ws: Path) -> tuple[int, list[Path]]:
    """Run `dumps_split.py input -o ws`; return (returncode, parts).

    On non-zero returncode, `parts` is [] (dumps_split.py aborted
    before writing anything when it hits the "no FILE blocks" case).
    On success, `parts` is every `<stem>_part<i>of<N>.md` file in
    `ws` — length 1 for a fit-in-one-part dump, N for a real split.
    """
    cmd = [
        sys.executable, str(_SPLIT_SCRIPT),
        str(input_path),
        "-o", str(ws),
    ]
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(
            f"error: dumps_split.py is missing: {_SPLIT_SCRIPT}",
            file=sys.stderr,
        )
        return -1, []

    # Relay the split tool's diagnostics — they are the only way the
    # user can see why a "no FILE blocks" file was handled the way
    # it was.  Kept on stderr to leave our own stdout clean.
    for line in (r.stdout or "").splitlines():
        print(f"    {line}", file=sys.stderr)
    for line in (r.stderr or "").splitlines():
        print(f"    {line}", file=sys.stderr)

    if r.returncode != 0:
        return r.returncode, []
    return 0, _list_parts(ws)


def _run_pipe_as_is(input_path: Path) -> int:
    """Pipe the original file as a single chip."""
    print(
        f"[pipe] {input_path.name}: piping original as-is",
        file=sys.stderr,
    )
    return subprocess.run(
        _deeppeek_cmd() + ["pipe", str(input_path)],
    ).returncode


def _run_split_send(ws: Path) -> int:
    """Send the parts already present in `ws` via split_send's
    --workspace mode.  `progress.json` was written by the caller."""
    print(
        f"[split] sending parts from {ws} via `deeppeek prompt "
        f"split_send --workspace`",
        file=sys.stderr,
    )
    return subprocess.run(
        _deeppeek_cmd() + [
            "prompt", "split_send",
            "--workspace", str(ws),
        ],
    ).returncode


def _route_one(
    input_path: Path,
    args: argparse.Namespace,
) -> int:
    """Decide and act on one file.  Returns its exit code (0 ok, else
    failure)."""

    # ---- 1. try the split ----
    ws = Path(tempfile.mkdtemp(
        prefix="deeppeek_prompt_pipe_",
        dir=(args.tmp_base if args.tmp_base is not None else None),
    ))
    print(f"[workspace] {input_path.name} -> {ws}", file=sys.stderr)

    rc, parts = _try_split(input_path, ws)
    n_parts = len(parts)

    # ---- 2. classify ----
    #
    # Cases that must pipe the original as-is:
    #   · rc != 0       — "no FILE blocks parsed from ..." error
    #                     (dumps_split.py aborted; nothing written)
    #   · n_parts == 0  — defensive; should not happen on rc == 0
    #   · n_parts == 1  — the file fit inside a single part
    #                     (`<stem>_part1of1.md`); piping the part
    #                     would add a redundant PART 1/1 header
    #
    # Otherwise (rc == 0 and n_parts >= 2) we hand off to split_send.
    if rc != 0:
        reason = f"split exited {rc} (no FILE blocks / error)"
        if args.dry_run:
            print(
                f"[dry-run] {input_path.name}: would pipe as-is "
                f"({reason})",
                file=sys.stderr,
            )
            return 0
        return _run_pipe_as_is(input_path)

    if n_parts <= 1:
        reason = (
            "split produced 0 parts"
            if n_parts == 0
            else "split produced 1 part (fits in one chip)"
        )
        if args.dry_run:
            print(
                f"[dry-run] {input_path.name}: would pipe as-is "
                f"({reason})",
                file=sys.stderr,
            )
            return 0
        return _run_pipe_as_is(input_path)

    # ---- 3. multi-part path ----
    if args.dry_run:
        print(
            f"[dry-run] {input_path.name}: would split into "
            f"{n_parts} part(s) and send via split_send "
            f"--workspace {ws}",
            file=sys.stderr,
        )
        for p in parts:
            print(f"    part: {p.name}", file=sys.stderr)
        return 0

    _write_progress_json(ws, input_path, parts)
    return _run_split_send(ws)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    if not _SPLIT_SCRIPT.is_file():
        print(
            f"error: dumps_split.py not found at {_SPLIT_SCRIPT}\n"
            f"  this command needs a source checkout; reinstall with "
            f"`pipx install --editable /path/to/deeppeek --force`",
            file=sys.stderr,
        )
        return 3

    if args.tmp_base is not None:
        try:
            args.tmp_base.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(
                f"error: cannot create --tmp-base {args.tmp_base}: {e}",
                file=sys.stderr,
            )
            return 3

    failures: list[str] = []
    for raw in args.files:
        p = Path(raw).resolve()
        if not p.is_file():
            print(f"error: not a file: {p}", file=sys.stderr)
            failures.append(str(p))
            continue

        try:
            rc = _route_one(p, args)
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 1
        if rc != 0:
            failures.append(str(p))

    if failures:
        print(
            f"\n[prompt-pipe] {len(failures)} file(s) failed:",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1

    print(
        f"\n[prompt-pipe] all {len(args.files)} file(s) delivered.",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "pipe",
        help="Attach files, routing each through split_send when it "
             "splits into 2+ parts and falling back to a plain pipe "
             "when it does not",
    )
    p.add_argument(
        "files", nargs="+", type=Path, metavar="FILE",
        help="files to attach.  Each file is routed independently: "
             "dumps that split into 2+ parts go through "
             "`deeppeek prompt split_send` (part-by-part); small "
             "dumps, plain markdown, and files with no FILE blocks "
             "fall back to `deeppeek pipe` as a single chip.",
    )
    p.add_argument(
        "--tmp-base", type=Path, default=None, metavar="DIR",
        help="parent directory for the per-file scratch workspaces "
             "(default: system temp dir).  Each file gets its own "
             "subdirectory named `deeppeek_prompt_pipe_*`; the "
             "workspaces are left in place after the run so split "
             "parts remain inspectable.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="per file, print the routing decision (pipe-as-is vs. "
             "split_send --workspace) and stop.  Nothing is piped, "
             "nothing is sent.",
    )
    p.set_defaults(func=run)