"""Set or query the extension's watchdog setting.

The extension persists its watchdog preference in
browser.storage.local under the key `watchdog` (default: true).
This module edits that value by broadcasting a `config` SSE event
that the extension writes to storage, then ack-ing back via POST.

Requires the extension to be connected (Zen open with the extension
loaded).  If no extension is connected within 5 seconds, returns an
error — the CLI cannot write to browser storage directly.
"""

import queue
import sys
import time

from ..capture.server import ExportServer

CONNECT_TIMEOUT_S = 5.0
ACK_TIMEOUT_S = 3.0


def _wait_for_extension(server: ExportServer) -> bool:
    deadline = time.monotonic() + CONNECT_TIMEOUT_S
    while time.monotonic() < deadline:
        with server._lock:
            if server.sse_clients:
                return True
        time.sleep(0.05)
    return False


def _report_no_extension() -> None:
    print(
        "error: no deeppeek extension connected on "
        "http://127.0.0.1:8765/events",
        file=sys.stderr,
    )
    print(
        "  open Zen (with the extension loaded) and try again",
        file=sys.stderr,
    )


def set_watchdog(value: bool) -> int:
    try:
        server = ExportServer(port=8765, watch_mode=False)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(
                "error: another deeppeek CLI is running on port 8765",
                file=sys.stderr,
            )
            print(
                "  stop it first (or use the extension console directly: "
                "browser.storage.local.set({watchdog: %s}))" % ("true" if value else "false"),
                file=sys.stderr,
            )
            return 1
        raise

    try:
        if not _wait_for_extension(server):
            _report_no_extension()
            return 1

        server._broadcast("config", {"watchdog": value})

        try:
            response = server.wait_for_result(timeout=ACK_TIMEOUT_S)
        except queue.Empty:
            print(
                "error: extension did not acknowledge the write "
                "(check the extension console for errors)",
                file=sys.stderr,
            )
            return 1

        if response.get("__ack") != "watchdog":
            print(
                f"error: unexpected ack from extension: {response!r}",
                file=sys.stderr,
            )
            return 1

        actual = response.get("watchdog")
        if actual != value:
            print(
                f"warning: extension reports watchdog={actual}, "
                f"expected {value}",
                file=sys.stderr,
            )
            return 1

        state = "ON" if value else "OFF"
        print(f"Watchdog {state} (persisted in browser.storage.local)")
        return 0
    finally:
        server.stop()


def get_status() -> int:
    try:
        server = ExportServer(port=8765, watch_mode=False)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(
                "error: another deeppeek CLI is running on port 8765",
                file=sys.stderr,
            )
            print("  stop it first to query the extension", file=sys.stderr)
            return 1
        raise

    try:
        if not _wait_for_extension(server):
            _report_no_extension()
            return 1

        server._broadcast("request-status")

        try:
            response = server.wait_for_result(timeout=ACK_TIMEOUT_S)
        except queue.Empty:
            print(
                "error: extension did not respond to status request",
                file=sys.stderr,
            )
            return 1

        if response.get("__status") != "watchdog":
            print(
                f"error: unexpected status reply: {response!r}",
                file=sys.stderr,
            )
            return 1

        value = response.get("watchdog")
        state = "ON" if value else "OFF"
        print(f"Watchdog: {state}")
        return 0
    finally:
        server.stop()
