"""`deeppeek prompt delete` — clear the composer.

Removes all attached file chips and empties the textarea in the active
DeepSeek tab.

Files are removed by invoking the React `onClick` handler on the
remove button directly (no hover simulation), and the textarea is
cleared via the native value setter. See `injected.js` Part 9.
"""

from __future__ import annotations

import argparse
import sys

from ..commands._composer import broadcast_and_wait, composer_session


def run(args: argparse.Namespace) -> int:
    print("Clearing composer...", file=sys.stderr)

    with composer_session(port=args.port, timeout=args.timeout) as server:
        if server is None:
            return 1

        rc, result = broadcast_and_wait(
            server, "clear", {},
            timeout=args.timeout, ack_name="clear",
        )

        if rc == 0:
            removed = result.get("files_removed", 0)
            cleared = result.get("textarea_cleared", False)
            print(f"OK — textarea {'cleared' if cleared else 'empty already'}, "
                  f"{removed} file(s) removed", file=sys.stderr)
            return 0

        if rc == 2:
            print(f"Clear failed: {result.get('reason') or '(no reason)'}",
                  file=sys.stderr)
            return 2

        return rc


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "delete",
        help="Remove all attached files and clear the composer textarea",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--timeout", type=float, default=30.0,
        help="seconds for extension connection AND the clear ack (default: 30)",
    )
    p.set_defaults(func=run)
