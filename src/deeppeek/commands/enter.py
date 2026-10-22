"""`deeppeek enter` — press Enter on the DeepSeek composer.

Submits whatever text is already in the composer, whether it was set
by `deeppeek type`, attached by `deeppeek pipe`, or typed by hand.
Fails fast if the composer is empty — no-op Enters just confuse.
"""
import argparse
import sys

from ._composer import broadcast_and_wait, composer_session


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "enter",
        help="Press Enter on the DeepSeek composer (submits typed text)",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="how long to wait for the extension to connect and ack "
             "(default: 30)",
    )
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    print("Pressing Enter on composer...", file=sys.stderr)

    with composer_session(port=args.port, timeout=args.timeout) as server:
        if server is None:
            return 1

        rc, result = broadcast_and_wait(
            server, "enter", {},
            timeout=args.timeout, ack_name="enter",
        )

        if rc == 0:
            sel = result.get("composer_selector") or "(unknown)"
            print(f"OK — submitted via {sel}", file=sys.stderr)
            return 0
        if rc == 2:
            print(f"Enter failed: {result.get('reason') or '(no reason)'}",
                  file=sys.stderr)
            for k in ("composer_selector", "submitted",
                      "composer_cleared"):
                if k in result:
                    print(f"  {k}: {result[k]}", file=sys.stderr)
            return 2
        return rc
