"""`deeppeek pipe` — attach files to the active DeepSeek tab.

Reads each file, base64-encodes it, waits for the extension's SSE
connection, then broadcasts an `attach` event.  The extension forwards
to the content script, which calls `__dsx_attach_json` in the page
world (`dragover`+`drop` on the textarea's parent, with a
`file-input` fallback), then POSTs an ack back.

Directory entries in the argument list are skipped with a note —
this makes `deeppeek pipe dir/*` safe even when `dir/` contains
subdirectories (e.g. `__pycache__`).  Non-existent paths still abort
with an error, so typos are caught.

Constraint: only one CLI can run on port 8765 at a time.  If a
`capture --watch` (or another `pipe`) is already running, this
command fails with a clear error.

Dispatch: if a `deeppeek daemon` is running on the target port,
this command routes through its `/internal/relay` endpoint instead
of opening a local `ExportServer`.  Otherwise it falls back to the
local-server path (the original behaviour).
"""
import argparse
import base64
import mimetypes
import sys
from pathlib import Path

from ._composer import submit_request


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "pipe",
        help="Attach files to the active DeepSeek tab (via the extension)",
    )
    p.add_argument(
        "files", nargs="+", type=Path, metavar="FILE",
        help="files to attach (directories in the list are skipped)",
    )
    p.add_argument(
        "--serial", action="store_true",
        help="attach one at a time with --delay between each "
             "(default: batch, all files in one drop event)",
    )
    p.add_argument(
        "--delay", type=float, default=0.3, metavar="SECONDS",
        help="delay between files in --serial mode (default: 0.3)",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="how long to wait for the extension to connect and "
             "acknowledge (default: 30)",
    )
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=run)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def run(args: argparse.Namespace) -> int:
    # ---- read + encode files before opening the server --------------
    payload_files: list[dict] = []
    skipped_dirs: list[Path] = []
    print(f"Reading {len(args.files)} file(s)...", file=sys.stderr)
    total_raw = 0
    for f in args.files:
        if not f.exists():
            print(f"error: no such file: {f}", file=sys.stderr)
            return 1
        if not f.is_file():
            # Directory (e.g. __pycache__ from a `dir/*` glob) or
            # special file.  Skip — do not abort — because shell
            # globbing routinely surfaces these in the arg list.
            skipped_dirs.append(f)
            continue
        try:
            data = f.read_bytes()
        except OSError as e:
            print(f"error: cannot read {f}: {e}", file=sys.stderr)
            return 1
        mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        b64 = base64.b64encode(data).decode("ascii")
        payload_files.append({"name": f.name, "mime": mime, "base64": b64})
        total_raw += len(data)
        print(f"  {f.name} ({_human_size(len(data))}, {mime})", file=sys.stderr)

    if skipped_dirs:
        for d in skipped_dirs:
            print(f"  skipped (not a regular file): {d}", file=sys.stderr)

    if not payload_files:
        print("error: no attachable files in the argument list",
              file=sys.stderr)
        return 1

    b64_total = sum(len(f["base64"]) for f in payload_files)
    print(f"Total raw: {_human_size(total_raw)} "
          f"→ base64: {_human_size(b64_total)}", file=sys.stderr)

    # ---- dispatch via daemon relay or local ExportServer --------
    delay_ms = int(args.delay * 1000) if args.serial else 0
    mode = "serial" if args.serial else "batch"
    print(f"Attaching {len(payload_files)} file(s) in {mode} mode...",
          file=sys.stderr)

    rc, result = submit_request(
        "attach",
        {"files": payload_files, "opts": {"delay_ms": delay_ms}},
        timeout=args.timeout, ack_name="attach",
        port=args.port,
    )

    if rc == 1:
        return 1

    if rc == 2:
        print(f"Attach failed: {result.get('reason') or '(no reason given)'}",
              file=sys.stderr)
        dispatched = int(result.get("dispatched") or 0)
        total = int(result.get("total") or len(payload_files))
        previews_before = int(result.get("previews_before") or 0)
        previews_after = int(result.get("previews_after") or 0)
        print(f"  dispatched: {dispatched}/{total}, "
              f"previews added: {previews_after - previews_before}",
              file=sys.stderr)
        return 2

    # rc == 0
    method = result.get("method") or "(unknown)"
    dispatched = int(result.get("dispatched") or 0)
    total = int(result.get("total") or len(payload_files))
    previews_before = int(result.get("previews_before") or 0)
    previews_after = int(result.get("previews_after") or 0)
    previews_added = previews_after - previews_before
    print(f"Attached {dispatched}/{total} file(s) via {method}.",
          file=sys.stderr)
    if previews_added > 0:
        print(f"  {previews_added} new preview(s) rendered in the "
              f"chat input.", file=sys.stderr)
    elif previews_added == 0 and total > 0:
        print(f"  Warning: no previews appeared.  The drop was "
              f"accepted but DeepSeek may have rejected the file(s).",
              file=sys.stderr)
    return 0