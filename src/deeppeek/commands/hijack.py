"""`deeppeek hijack` — redirect code-block downloads to cwd.

Opens an SSE connection to the extension, tells it to install a
click interceptor on every code-block "Download" button in the
active DeepSeek tab, and then writes every intercepted download to
the current working directory instead of the browser's Downloads
folder.

The extension side is `injected.js` Part 12; the interceptor is a
capture-phase click listener that fires before React's handler and
preventDefault()s the browser download. The file content is
POSTed back as base64 and written here.

    deeppeek hijack                  # write to cwd, flat
    deeppeek hijack --dir ./tmp      # write to ./tmp, flat
    deeppeek hijack --preserve-path  # keep declared subdirectories
    # Ctrl+C to stop (uninstalls the interceptor)

By default, only the basename of a declared path is kept: a block
whose prose says `utils/paths.py` writes to `<dir>/paths.py`. Pass
`--preserve-path` to keep the directory structure (creating
subdirectories as needed). Stripping is the safe default because a
declared path could otherwise land files in unexpected places; the
basename cannot escape `<dir>`.
"""
import argparse
import base64
import queue
import sys
import uuid
from pathlib import Path

from ..capture.server import ExportServer


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "hijack",
        help="Intercept DeepSeek code-block download buttons; write "
             "files to the current directory instead of ~/Downloads",
    )
    p.add_argument(
        "--dir", type=Path, default=None, metavar="PATH",
        help="directory to write intercepted files into "
             "(default: current working directory)",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="extension SSE port (default: 8765)",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="how long to wait for the extension to connect (default: 30)",
    )
    p.add_argument(
        "--preserve-path", action="store_true",
        help="keep directory paths declared in the AI's prose. "
             "A block declared as `utils/paths.py` writes to "
             "<dir>/utils/paths.py (subdirectories created as "
             "needed). Default: flatten to basename, so the same "
             "block writes to <dir>/paths.py. Flattening is safe "
             "by construction: the basename has no path components, "
             "so a malicious or confused declaration cannot escape "
             "<dir>.",
    )
    p.set_defaults(func=run)


def _safe_relative_path(filename: str) -> Path:
    """Sanitize a declared filename into a relative path under out_dir.

    The recover pipeline's `header_path` may include subdirectories
    (`playgrounds/inpaint/pg_base.py`); we preserve them, matching
    the `--apply-to` behaviour. `..` components are refused so a
    malicious or confused filename cannot escape the output dir.
    """
    parts = []
    for part in (filename or "").replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError(
                f"filename escapes output dir: {filename!r}")
        parts.append(part)
    if not parts:
        raise ValueError(
            f"empty filename after sanitization: {filename!r}")
    return Path(*parts)


def _dedup_target(target: Path) -> Path:
    """Return a non-colliding sibling path: name (1).ext, name (2).ext."""
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    n = 1
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _send_and_wait(server, event: str, ack_name: str,
                   timeout: float) -> dict:
    batch_id = uuid.uuid4().hex[:12]
    server._broadcast(event, {"batch_id": batch_id})
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
            f"error: batch_id mismatch for {ack_name}: "
            f"{ack.get('batch_id')!r} != {batch_id!r}")
    return ack.get("result") or {}


def run(args: argparse.Namespace) -> int:
    out_dir = (args.dir if args.dir is not None else Path.cwd()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    preserve_path = bool(args.preserve_path)

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

    installed = False
    try:
        print(f"Waiting for extension on http://127.0.0.1:{args.port}/events "
              f"(up to {args.timeout:.0f}s)...", file=sys.stderr)
        if not server.wait_for_extension(timeout=args.timeout):
            print("error: no extension connected within timeout.",
                  file=sys.stderr)
            print("  make sure Zen is open, the extension is loaded, and a "
                  "deepseek.com tab is active.", file=sys.stderr)
            return 1

        result = _send_and_wait(
            server, "hijack-install", "hijack-install", args.timeout)
        if not result.get("success"):
            reason = result.get("reason") or "(no reason)"
            print(f"error: hijack install failed: {reason}",
                  file=sys.stderr)
            return 2
        installed = True

        mode_desc = (
            "preserve-path" if preserve_path else "flat (basename only)"
        )
        print(f"\nHijacker installed.", file=sys.stderr)
        print(f"Mode: {mode_desc}", file=sys.stderr)
        print(f"Downloads will be written to: {out_dir}", file=sys.stderr)
        print(f"Click any code-block 'Download' button in the DeepSeek tab.",
              file=sys.stderr)
        print(f"Press Ctrl+C to stop and uninstall.\n", file=sys.stderr)

        n_written = 0
        while True:
            ack = server.wait_for_result(timeout=None)
            if ack.get("__ack") != "download":
                continue

            filename = ack.get("filename") or "download.bin"
            b64 = ack.get("base64") or ""
            source = ack.get("source") or "?"
            try:
                data = base64.b64decode(b64)
            except Exception as e:
                print(f"  [skip] {filename}: invalid base64 ({e})",
                      file=sys.stderr)
                continue

            if preserve_path:
                try:
                    rel = _safe_relative_path(filename)
                except ValueError as e:
                    print(f"  [skip] {filename}: {e}",
                          file=sys.stderr)
                    continue
            else:
                # Default: flatten to basename. Safe by construction
                # (basename has no path components), so a malicious
                # or confused declaration cannot escape out_dir.
                norm = (filename or "").replace("\\", "/")
                name = Path(norm).name
                if not name or name in (".", ".."):
                    name = "download.bin"
                rel = Path(name)

            target = _dedup_target(out_dir / rel)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as e:
                print(f"  [error] {filename}: write failed ({e})",
                      file=sys.stderr)
                continue

            n_written += 1
            try:
                display = target.relative_to(out_dir)
            except ValueError:
                display = target
            flattened = (
                not preserve_path
                and filename
                and ("/" in filename or "\\" in filename)
            )
            note = f", declared {filename}" if flattened else ""
            print(f"  [{n_written}] {display} "
                  f"({len(data):,} bytes, source={source}{note})",
                  file=sys.stderr)

    except KeyboardInterrupt:
        print("\nStopping...", file=sys.stderr)
        return 0
    finally:
        if installed:
            try:
                _send_and_wait(
                    server, "hijack-uninstall", "hijack-uninstall", 3.0)
            except (SystemExit, queue.Empty):
                pass
        server.stop()
