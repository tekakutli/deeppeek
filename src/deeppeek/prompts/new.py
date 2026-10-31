"""`deeppeek prompt new` — open a new tab at chat.deepseek.com.

Two paths, tried in order:

  1. **Extension SSE** — broadcast an `open-tab` event; the
     extension's background script calls
     `browser.tabs.create({url, active: true})` and acks with the
     new tab id.  This is the primary path when the extension is
     connected (a DeepSeek tab is open, or the extension's watchdog
     has connected for a recent CLI command).

  2. **Direct launch** — if the extension does not connect within
     `--extension-timeout` seconds, fall back to launching the URL
     through the `zen` (or `zen-browser`) binary, then
     `webbrowser.open`.  The tab opens either way; the extension
     path additionally lets the extension know about the new tab,
     which is not required but is tidy.

    deeppeek prompt new                          # extension, then fallback
    deeppeek prompt new --direct                 # skip the extension
    deeppeek prompt new --url https://...        # custom URL
    deeppeek prompt new --extension-timeout 1    # fail over faster

Exit codes:
    0  tab opened (via extension or direct)
    1  both paths failed
    2  extension connected but reported an error opening the tab
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import webbrowser

from ..capture.server import ExportServer
from ..commands._composer import broadcast_and_wait


DEFAULT_URL = "https://chat.deepseek.com/"


def _open_direct(url: str) -> int:
    """Launch the URL without the extension.

    Preference order:
      1. `zen` on PATH        — matches deeppeek's target browser
      2. `zen-browser` on PATH — alternative binary name
      3. `webbrowser.open`    — OS default handler
    """
    for candidate in ("zen", "zen-browser"):
        path = shutil.which(candidate)
        if path:
            print(f"  launching: {path} --new-tab {url}",
                  file=sys.stderr)
            try:
                subprocess.run(
                    [path, "--new-tab", url],
                    check=False,
                )
                return 0
            except OSError as e:
                print(f"  {candidate} failed: {e}", file=sys.stderr)
                # fall through to webbrowser

    print(f"  launching via webbrowser.open({url!r})",
          file=sys.stderr)
    try:
        ok = webbrowser.open(url)
    except Exception as e:
        print(f"error: webbrowser.open threw: {e}", file=sys.stderr)
        return 1
    if not ok:
        print("error: webbrowser.open returned False — no handler "
              "for the URL", file=sys.stderr)
        return 1
    return 0


def _open_via_extension(url: str, port: int, timeout: float) -> int | None:
    """Try to open the tab via the extension.

    Returns:
      0    — extension acked success
      2    — extension reported `success: false`
      None — extension not connected, or infra failure — the caller
             should fall back to `_open_direct`
    """
    try:
        server = ExportServer(port=port)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"  port {port} in use — another deeppeek command "
                  f"running; falling back", file=sys.stderr)
            return None
        raise

    try:
        # Silent wait: an absent extension is a normal, expected case
        # here — we're about to fall back.  Do NOT use
        # `composer_session`, whose "no extension connected" message
        # is appropriate for composer commands but misleading for
        # this one.
        if not server.wait_for_extension(timeout=timeout):
            return None

        rc, result = broadcast_and_wait(
            server, "open-tab", {"url": url},
            timeout=timeout, ack_name="open-tab",
        )
        if rc == 0:
            tab_id = result.get("tab_id")
            print(f"OK — tab opened via extension (id={tab_id})",
                  file=sys.stderr)
            return 0
        if rc == 2:
            print(f"extension error: "
                  f"{result.get('reason') or '(no reason)'}",
                  file=sys.stderr)
            return 2
        # rc == 1: infrastructure failure.  Treat it like
        # "not connected" — fall back.
        return None
    finally:
        server.stop()


def run(args: argparse.Namespace) -> int:
    if args.direct:
        return _open_direct(args.url)

    print(f"Opening {args.url}...", file=sys.stderr)
    rc = _open_via_extension(args.url, args.port, args.extension_timeout)
    if rc is not None:
        return rc

    print(f"  extension not connected within "
          f"{args.extension_timeout:.0f}s — falling back",
          file=sys.stderr)
    return _open_direct(args.url)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "new",
        help="Open a new tab at chat.deepseek.com (via the extension "
             "if connected, otherwise by launching Zen directly)",
    )
    p.add_argument(
        "--url", default=DEFAULT_URL,
        help=f"URL to open (default: {DEFAULT_URL})",
    )
    p.add_argument(
        "--direct", action="store_true",
        help="skip the extension and launch the browser directly "
             "(zen --new-tab, then webbrowser.open)",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="extension SSE port (default: 8765)",
    )
    p.add_argument(
        "--extension-timeout", type=float, default=3.0,
        metavar="SECONDS",
        help="how long to wait for the extension to connect before "
             "falling back to a direct launch (default: 3)",
    )
    p.set_defaults(func=run)
