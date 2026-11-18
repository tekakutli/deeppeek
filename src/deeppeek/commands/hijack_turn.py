"""`deeppeek hijack_turn` — redirect Copy Turn buttons to write .md files.

Opens an SSE connection to the extension, tells it to arm the injected
`Copy Turn` buttons so that clicking one POSTs the resolved turn's
markdown back here (instead of writing to the clipboard), and writes
each intercepted turn to a .md file under a per-invocation timestamped
subdirectory.

    deeppeek hijack_turn                  # → /tmp/20261117_143022/
    deeppeek hijack_turn --dir ./turns    # → ./turns/20261117_143022/
    # Ctrl+C to stop (uninstalls the arming)

Filenames are generated on the extension side as
`<title-slug>_turn_<N>_of_<T>.md`; if the chat title is empty the slug
is omitted (`turn_<N>_of_<T>.md`).

Each invocation creates a fresh timestamped subdirectory, so re-running
`hijack_turn` never overwrites a previous session's files.  Matches the
daemon-style `deeppeek editor` convention (Q6, 2026-11-17).  The
resolved subdirectory path is printed at startup so the user always
knows where to look.

The CLI side is a thin install/uninstall broadcast plus a write loop.
The extension side (2026-11-17):
  · `bg-sse.js` listens for `hijack-turn-install` /
    `hijack-turn-uninstall`, dispatched to `bg-handlers-hijack.js`
  · those handlers forward to the content script as
    `DSX_HIJACK_TURN_SET` / `DSX_HIJACK_TURN_CLEAR`, which postMessage
    to the page world (`{dsx_hijack_turn_set: {port: <n>|null}}`)
  · `05-copy-turn.js` picks that up, toggles hijack mode, and POSTs
    each clicked turn directly to this command's `/export` endpoint
    (same route + ack shape as the working `hijack` click path)
"""
import argparse
import base64
import queue
import re
import sys
import time
import uuid
from pathlib import Path

from ..transport.server import ExportServer


def _safe_name(filename: str) -> str:
    """Basename-only sanitization: no path separators, no control
    characters, no shell metas."""
    norm = (filename or "").replace("\\", "/")
    name = Path(norm).name
    if not name or name in (".", ".."):
        name = "turn.md"
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)
    return name


def _write_turn_ack(ack: dict, out_dir: Path, n_written: int) -> int:
    filename = _safe_name(ack.get("filename") or "turn.md")
    b64 = ack.get("base64") or ""
    position = ack.get("position") or {}

    try:
        data = base64.b64decode(b64)
    except Exception as e:
        print(f"  [skip] {filename}: invalid base64 ({e})", file=sys.stderr)
        return n_written

    target = out_dir / filename
    existed = target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as e:
        print(f"  [error] {filename}: write failed ({e})", file=sys.stderr)
        return n_written

    n_written += 1
    turn = position.get("turn", "?")
    total = position.get("totalTurns", "?")
    method = position.get("method", "?")
    note = ", overwrote" if existed else ""
    print(
        f"  [{n_written}] {filename} "
        f"({len(data):,} bytes, turn {turn}/{total}, {method}{note})",
        file=sys.stderr,
    )
    return n_written


def _send_and_wait(server, event: str, ack_name: str,
                   timeout: float, extra: dict = None) -> dict:
    """Broadcast `event`, wait for the matching ack, return its
    `result` payload.  `extra` keys are merged into the broadcast
    payload — used by `hijack-turn-install` to pass `port` so the
    page-world fetch knows which URL to hit."""
    batch_id = uuid.uuid4().hex[:12]
    payload = {"batch_id": batch_id}
    if extra:
        payload.update(extra)
    server._broadcast(event, payload)
    try:
        ack = server.wait_for_result(timeout=timeout)
    except queue.Empty:
        raise SystemExit(
            f"error: extension did not acknowledge {ack_name} within "
            f"{timeout:.0f}s")
    if ack.get("__ack") != ack_name:
        raise SystemExit(
            f"error: unexpected ack for {ack_name}: {ack!r}")
    if ack.get("batch_id") != batch_id:
        raise SystemExit(
            f"error: batch_id mismatch for {ack_name}")
    return ack.get("result") or {}


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "hijack_turn",
        help="Intercept Copy Turn buttons; write each clicked turn "
             "to a .md file in a fresh timestamped directory "
             "instead of copying to clipboard",
    )
    p.add_argument(
        "--dir", type=Path, default=None, metavar="PATH",
        help="parent directory for the per-invocation session "
             "subdirectory (default: /tmp).  Each run creates "
             "PATH/<YYYYMMDD_HHMMSS>/ so re-runs never overwrite "
             "earlier sessions.",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="extension SSE port (default: 8765)",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="how long to wait for extension connection (default: 30)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    # Q6: each invocation gets a fresh timestamped subdir so
    # re-running `hijack_turn` never overwrites earlier sessions.
    # With --dir, the subdir sits inside the given dir
    # (./out/20261117_143022/); without --dir, directly under /tmp.
    base = (args.dir if args.dir is not None else Path("/tmp")).resolve()
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = base / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Session dir: {out_dir}", file=sys.stderr)

    try:
        server = ExportServer(port=args.port)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(
                f"error: port {args.port} is already in use — is "
                f"another `deeppeek` command running?",
                file=sys.stderr)
            print(
                "  stop it first (Ctrl+C the other command), then retry.",
                file=sys.stderr)
            return 1
        raise

    installed = False
    try:
        print(
            f"Waiting for extension on http://127.0.0.1:{args.port}/events "
            f"(up to {args.timeout:.0f}s)...",
            file=sys.stderr,
        )
        if not server.wait_for_extension(timeout=args.timeout):
            print("error: no extension connected within timeout.",
                  file=sys.stderr)
            print(
                "  make sure Zen is open, the extension is loaded, and a "
                "deepseek.com tab is active.",
                file=sys.stderr)
            return 1

        # Pass `port` in the install broadcast so the page-world
        # fetch (in 05-copy-turn.js) knows where to POST downloads.
        result = _send_and_wait(
            server, "hijack-turn-install", "hijack-turn-install",
            args.timeout, extra={"port": args.port})
        if not result.get("success"):
            reason = result.get("reason") or "(no reason)"
            print(f"error: hijack-turn install failed: {reason}",
                  file=sys.stderr)
            return 2
        installed = True

        print(f"\nHijacker (turn) installed.", file=sys.stderr)
        print(f"Turns will be written to: {out_dir}", file=sys.stderr)
        print(
            f"Click any 'Copy Turn' button in the DeepSeek tab.",
            file=sys.stderr)
        print(f"Press Ctrl+C to stop and uninstall.\n", file=sys.stderr)

        n_written = 0
        while True:
            ack = server.wait_for_result(timeout=None)
            if ack.get("__ack") != "download":
                continue
            n_written = _write_turn_ack(ack, out_dir, n_written)

    except KeyboardInterrupt:
        print("\nStopping...", file=sys.stderr)
        return 0
    finally:
        if installed:
            # Fire-and-forget uninstall — do NOT wait for an ack,
            # which would race with in-flight `download` acks in
            # the same result_queue.  0.3s sleep lets the SSE
            # writer thread flush before server.stop() closes the
            # socket.
            try:
                server._broadcast(
                    "hijack-turn-uninstall",
                    {"batch_id": "shutdown"},
                )
                time.sleep(0.3)
            except BaseException:
                pass
        server.stop()
