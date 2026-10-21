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
"""
import argparse
import base64
import mimetypes
import queue
import sys
import uuid
from pathlib import Path

from ..capture.server import ExportServer


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

    # ---- open server -----------------------------------------------
    try:
        server = ExportServer(port=args.port)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"error: port {args.port} is already in use — is "
                  f"another `deeppeek` command running?", file=sys.stderr)
            print("  stop it first (Ctrl+C the other command), then retry.",
                  file=sys.stderr)
            return 1
        raise

    try:
        print(f"Waiting for extension on http://127.0.0.1:{args.port}/events "
              f"(up to {args.timeout:.0f}s)...", file=sys.stderr)
        if not server.wait_for_extension(timeout=args.timeout):
            print("error: no extension connected within timeout.",
                  file=sys.stderr)
            print("  make sure Zen is open, the extension is loaded, and a "
                  "deepseek.com tab is available.", file=sys.stderr)
            return 1

        batch_id = uuid.uuid4().hex[:12]
        delay_ms = int(args.delay * 1000) if args.serial else 0
        mode = "serial" if args.serial else "batch"
        print(f"Extension connected. Attaching {len(payload_files)} "
              f"file(s) in {mode} mode...", file=sys.stderr)

        server.broadcast_attach(payload_files, batch_id, delay_ms=delay_ms)

        try:
            ack = server.wait_for_result(timeout=args.timeout)
        except queue.Empty:
            print("error: extension did not acknowledge within timeout.",
                  file=sys.stderr)
            return 1

        if ack.get("__ack") != "attach":
            print(f"error: unexpected response: {ack!r}", file=sys.stderr)
            return 1
        if ack.get("batch_id") != batch_id:
            print(f"error: response batch_id mismatch "
                  f"(got {ack.get('batch_id')}, expected {batch_id})",
                  file=sys.stderr)
            return 1

        result = ack.get("result") or {}
        success = bool(result.get("success"))
        method = result.get("method") or "(unknown)"
        dispatched = int(result.get("dispatched") or 0)
        total = int(result.get("total") or len(payload_files))
        previews_before = int(result.get("previews_before") or 0)
        previews_after = int(result.get("previews_after") or 0)
        previews_added = previews_after - previews_before
        reason = result.get("reason")

        if success:
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

        print(f"Attach failed: {reason or '(no reason given)'}",
              file=sys.stderr)
        print(f"  dispatched: {dispatched}/{total}, "
              f"previews added: {previews_added}", file=sys.stderr)
        return 2
    finally:
        server.stop()
