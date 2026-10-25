"""`deeppeek type` — write text into the DeepSeek composer.

Does not submit.  Pair with `deeppeek enter` to submit separately, or
with a future combined command for a single pass.  The text is set
via `execCommand('insertText')` in the page world, which is the only
path React's onChange accepts on the current DeepSeek build — see the
comment block in `extension/assets/injected.js` Part 8.

Composer targeting uses `document.activeElement` first, falling back
to the strategy list.  See `findComposer` in `injected.js` for the
priority order.  The ack carries `composer_source` so the caller can
tell *how* the element was chosen.
"""
import argparse
import sys
from pathlib import Path

from ._composer import broadcast_and_wait, composer_session


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "type",
        help="Type text into the DeepSeek composer (does not submit)",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "file", nargs="?", type=Path, metavar="FILE",
        help="file whose UTF-8 contents become the composer text",
    )
    src.add_argument("--text", help="literal text to type")
    p.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="how long to wait for the extension to connect and ack "
             "(default: 30)",
    )
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=run)


def _read_text(args) -> str | None:
    if args.text is not None:
        return args.text
    if not args.file.exists():
        print(f"error: no such file: {args.file}", file=sys.stderr)
        return None
    if not args.file.is_file():
        print(f"error: not a regular file: {args.file}", file=sys.stderr)
        return None
    try:
        return args.file.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read {args.file}: {e}", file=sys.stderr)
        return None
    except UnicodeDecodeError as e:
        print(f"error: {args.file} is not UTF-8: {e}", file=sys.stderr)
        return None


def run(args: argparse.Namespace) -> int:
    text = _read_text(args)
    if text is None:
        return 1
    if not text.strip():
        print("error: text is empty", file=sys.stderr)
        return 1

    print(f"Typing {len(text):,} chars...", file=sys.stderr)

    with composer_session(port=args.port, timeout=args.timeout) as server:
        if server is None:
            return 1

        rc, result = broadcast_and_wait(
            server, "type", {"text": text},
            timeout=args.timeout, ack_name="type",
        )

        if rc == 0:
            sel = result.get("composer_selector") or "(unknown)"
            source = result.get("composer_source") or "?"
            print(f"OK — {len(text):,} chars in composer via "
                  f"{sel} (source={source})", file=sys.stderr)
            return 0
        if rc == 2:
            print(f"Type failed: {result.get('reason') or '(no reason)'}",
                  file=sys.stderr)
            for k in ("composer_selector", "composer_source",
                      "react_accepted",
                      "composer_element_info", "active_element_info"):
                if k in result:
                    print(f"  {k}: {result[k]}", file=sys.stderr)
            return 2
        return rc
