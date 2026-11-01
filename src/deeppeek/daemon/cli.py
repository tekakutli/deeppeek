"""``deeppeek daemon`` — argparse registration + run."""
import argparse
import sys

from ..capture.server import ExportServer
from .bridge import BrowserBridge
from .server import serve


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "daemon",
        help="Run an OpenAI-compatible model server backed by the "
             "browser extension (aider can dial into it)",
    )
    p.add_argument(
        "--port", type=int, default=8766,
        help="OpenAI-compatible HTTP port (default: 8766)",
    )
    p.add_argument(
        "--extension-port", type=int, default=8765,
        help="extension SSE port (default: 8765)",
    )
    p.add_argument(
        "--reuse-tab", action="store_true",
        help="reuse the current DeepSeek tab instead of opening a "
             "new one per request.  WARNING: the model sees the "
             "tab's prior conversation as context — usually NOT "
             "what you want with aider, which resends full history "
             "in each request.  Default: fresh tab per request.  "
             "Incompatible with --container / --container-id (no "
             "new tab is opened, so no container to select).",
    )
    p.add_argument(
        "--container", default=None, metavar="NAME",
        help="open new DeepSeek tabs inside the Firefox/Zen "
             "Container Tab whose display name matches NAME "
             "(case-insensitive; exact match preferred).  Mutually "
             "exclusive with --container-id and --reuse-tab.",
    )
    p.add_argument(
        "--container-id", default=None, metavar="COOKIE_STORE_ID",
        help="open new DeepSeek tabs inside the container with this "
             "raw cookieStoreId (e.g. 'firefox-container-3').  "
             "Skips the name lookup.  Mutually exclusive with "
             "--container and --reuse-tab.",
    )
    p.add_argument(
        "--tab-wait", type=float, default=2.0,
        help="seconds to wait after opening a new tab before "
             "submitting (default: 2.0)",
    )
    p.add_argument(
        "--connect-timeout", type=float, default=30.0,
        help="seconds to wait for the extension to connect "
             "(default: 30)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    # ---- argument validation (before any resources are bound) ----
    #
    # Container flags were added alongside the same flags on
    # `prompt new`.  Three failure modes need to be caught here
    # rather than silently ignored:
    #
    #   · --container and --container-id together
    #   · either flag with an empty / whitespace-only value
    #   · either flag with --reuse-tab (no new tab is opened, so
    #     the container choice has nowhere to go)
    #
    # All return exit code 3, matching the argument-error convention
    # in `prompt new`, `capture`, and `recover`.
    if args.container is not None and args.container_id is not None:
        print("error: --container and --container-id are mutually "
              "exclusive", file=sys.stderr)
        return 3
    if args.container is not None and not args.container.strip():
        print("error: --container is empty (whitespace-only)",
              file=sys.stderr)
        return 3
    if args.container_id is not None and not args.container_id.strip():
        print("error: --container-id is empty (whitespace-only)",
              file=sys.stderr)
        return 3
    if ((args.container is not None or args.container_id is not None)
            and args.reuse_tab):
        print("error: --container/--container-id cannot be combined "
              "with --reuse-tab (no new tab is opened, so there is "
              "no container to select)", file=sys.stderr)
        return 3

    try:
        ext = ExportServer(port=args.extension_port)
        ext.start()
    except OSError as e:
        print(
            f"error: cannot bind extension port {args.extension_port}: {e}",
            file=sys.stderr,
        )
        print(
            "  another deeppeek command may be running; Ctrl+C it first",
            file=sys.stderr,
        )
        return 1

    try:
        print(
            f"[daemon] waiting for extension on "
            f"http://127.0.0.1:{args.extension_port}/events "
            f"(up to {args.connect_timeout:.0f}s)...",
            file=sys.stderr,
        )
        if not ext.wait_for_extension(timeout=args.connect_timeout):
            print(
                "error: no extension connected within timeout",
                file=sys.stderr,
            )
            print(
                "  open Zen with the deeppeek extension loaded and a "
                "chat.deepseek.com tab active",
                file=sys.stderr,
            )
            return 1

        bridge = BrowserBridge(
            ext,
            new_tab=not args.reuse_tab,
            tab_wait=args.tab_wait,
            container=args.container,
            container_id=args.container_id,
        )

        base = f"http://127.0.0.1:{args.port}/v1"
        print("", file=sys.stderr)
        print(
            f"[daemon] OpenAI-compatible endpoint ready: {base}",
            file=sys.stderr,
        )
        if args.container:
            print(
                f"[daemon] new tabs in container: {args.container}",
                file=sys.stderr,
            )
        elif args.container_id:
            print(
                f"[daemon] new tabs in container-id: "
                f"{args.container_id}",
                file=sys.stderr,
            )
        print(f"[daemon]   export OPENAI_API_BASE={base}", file=sys.stderr)
        print(f"[daemon]   export OPENAI_API_KEY=dummy", file=sys.stderr)
        print(
            f"[daemon]   aider --model openai/deepseek-web",
            file=sys.stderr,
        )
        print("", file=sys.stderr)

        serve(bridge, port=args.port)
        return 0
    except KeyboardInterrupt:
        print("\n[daemon] stopping.", file=sys.stderr)
        return 0
    finally:
        ext.stop()
