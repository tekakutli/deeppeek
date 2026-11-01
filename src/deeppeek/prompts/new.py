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
     `webbrowser.open`.  Without a container flag, the tab opens
     either way; the extension path additionally lets the extension
     know about the new tab, which is not required but is tidy.

CONTAINER TABS
==============
`--container NAME` opens the new tab inside the Firefox/Zen
Container Tab whose display name matches NAME (case-insensitive;
exact match preferred).  `--container-id COOKIE_STORE_ID` takes a
raw cookieStoreId (e.g. `firefox-container-3`) and skips the name
lookup — useful when multiple containers share a display name, or
for scripting.

Container selection is only possible through the extension path:
the direct-launch fallback (`zen --new-tab`, `webbrowser.open`)
opens in the browser's default container and cannot be told to
use a specific one.  Therefore:

  · `--container` / `--container-id` + extension not connected
    → hard error (do NOT silently fall back to the default
    container; that would defeat the user's request)
  · `--direct` + `--container` / `--container-id`
    → argument error (direct launch cannot select a container)

The extension resolves the name to a cookieStoreId via
`browser.contextualIdentities.query({})` (which requires the
`contextualIdentities` permission in the manifest), filters
case-insensitively, and passes the result to `browser.tabs.create`.
If no container matches, the extension acks with
`success: false` and the available container names, so the error
is actionable.  Ambiguous case-insensitive matches are also
rejected with a `--container-id` hint.

    deeppeek prompt new                          # default container
    deeppeek prompt new --container Work         # container named "Work"
    deeppeek prompt new --container-id firefox-container-3
    deeppeek prompt new --direct                 # skip the extension
    deeppeek prompt new --url https://...        # custom URL
    deeppeek prompt new --extension-timeout 1    # fail over faster

Exit codes:
    0  tab opened (via extension or direct)
    1  both paths failed, OR a container flag was set but the
       extension never connected (the direct-launch fallback
       cannot honor a container choice)
    2  extension connected but reported an error opening the tab
       (e.g. no container named X, ambiguous match, or
       contextualIdentities unavailable in this browser)
    3  argument error (e.g. --container and --container-id
       together, --direct with a container flag, empty flag value)
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

    Note: this path cannot select a container.  It always opens in
    the browser's default container.
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


def _open_via_extension(
    url: str,
    port: int,
    timeout: float,
    *,
    container: str | None = None,
    container_id: str | None = None,
) -> int | None:
    """Try to open the tab via the extension.

    Returns:
      0    — extension acked success
      2    — extension reported `success: false` (e.g. no such
             container, ambiguous match, contextualIdentities
             unavailable)
      None — extension not connected, or infra failure — the caller
             decides whether to fall back to `_open_direct`
    """
    try:
        server = ExportServer(port=port)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"  port {port} in use — another deeppeek command "
                  f"running; cannot try the extension path",
                  file=sys.stderr)
            return None
        raise

    try:
        payload: dict = {"url": url}
        if container is not None:
            payload["container"] = container
        if container_id is not None:
            payload["container_id"] = container_id

        # Always show the wait message: it makes the failure mode
        # legible when a container flag is set (the fallback can't
        # honor a container, so "silent 3s wait then error" would
        # be confusing).
        print(f"  waiting up to {timeout:.0f}s for extension...",
              file=sys.stderr)
        if not server.wait_for_extension(timeout=timeout):
            return None

        rc, result = broadcast_and_wait(
            server, "open-tab", payload,
            timeout=timeout, ack_name="open-tab",
        )
        if rc == 0:
            tab_id = result.get("tab_id")
            cs_id = result.get("cookie_store_id")
            detail = f"id={tab_id}"
            if cs_id:
                detail += f", container={cs_id}"
            print(f"OK — tab opened via extension ({detail})",
                  file=sys.stderr)
            return 0
        if rc == 2:
            reason = result.get("reason") or "(no reason)"
            print(f"extension error: {reason}", file=sys.stderr)
            return 2
        # rc == 1: infrastructure failure.  Treat it like
        # "not connected" — caller may fall back.
        return None
    finally:
        server.stop()


def run(args: argparse.Namespace) -> int:
    # ---- argument validation ----
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

    container_flag = (args.container is not None
                      or args.container_id is not None)

    if args.direct and container_flag:
        print("error: --direct cannot select a container — the "
              "direct-launch path opens the URL in the browser's "
              "default container.  Drop --direct to use the "
              "extension path.", file=sys.stderr)
        return 3

    if args.direct:
        return _open_direct(args.url)

    target_desc = args.url
    if args.container:
        target_desc += f"  (container: {args.container})"
    elif args.container_id:
        target_desc += f"  (container-id: {args.container_id})"
    print(f"Opening {target_desc}...", file=sys.stderr)

    rc = _open_via_extension(
        args.url, args.port, args.extension_timeout,
        container=args.container,
        container_id=args.container_id,
    )
    if rc is not None:
        return rc

    # Extension not connected (rc is None).  Container flag means
    # we CANNOT fall back — direct launch has no way to select a
    # container, so silently opening in the default container
    # would defeat the user's request.
    if container_flag:
        print(
            f"error: the extension is not connected within "
            f"{args.extension_timeout:.0f}s.\n"
            f"  --container / --container-id require the extension "
            f"path — the direct-launch fallback cannot select a "
            f"container.\n"
            f"  · load the extension in Zen and reload the "
            f"DeepSeek tab, then retry\n"
            f"  · or drop the container flag to fall back to the "
            f"default container",
            file=sys.stderr,
        )
        return 1

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
        "--container", default=None, metavar="NAME",
        help="open the new tab inside the Firefox/Zen Container Tab "
             "whose display name matches NAME (case-insensitive; "
             "exact match preferred).  Requires the extension path "
             "— the direct-launch fallback cannot select a "
             "container.  Mutually exclusive with --container-id.",
    )
    p.add_argument(
        "--container-id", default=None, metavar="COOKIE_STORE_ID",
        help="open the new tab inside the container with this raw "
             "cookieStoreId (e.g. 'firefox-container-3').  Skips "
             "the name lookup — useful when multiple containers "
             "share a display name, or for scripting.  Mutually "
             "exclusive with --container.",
    )
    p.add_argument(
        "--direct", action="store_true",
        help="skip the extension and launch the browser directly "
             "(zen --new-tab, then webbrowser.open).  Incompatible "
             "with --container / --container-id (direct launch "
             "always opens in the default container).",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="extension SSE port (default: 8765)",
    )
    p.add_argument(
        "--extension-timeout", type=float, default=3.0,
        metavar="SECONDS",
        help="how long to wait for the extension to connect before "
             "failing (or, without a container flag, before falling "
             "back to a direct launch) (default: 3)",
    )
    p.set_defaults(func=run)
