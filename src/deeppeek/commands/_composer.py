"""Shared infrastructure for the composer commands.

`deeppeek type`, `deeppeek enter`, and `deeppeek pipe` all talk to the
extension over the same SSE transport: broadcast a typed event, wait
for a matching ack, report the result.  This module extracts that
pattern.

Two entry points:

    broadcast_and_wait(server, event, payload, timeout, ack_name)
        one event, one ack.  Used by `type` / `enter` / `pipe`.

    broadcast_many(server, specs, timeout)
        N events fired back-to-back on ONE connection, N acks
        collected and matched by batch_id.  Used by prompts that
        want the attachments and the composer text to land in
        parallel rather than one after the other.

The second form is what eliminates the port-8765 serialisation
between `pipe` and `type` that the subprocess-based prompts
inherit.

`submit_request` is the top-level dispatch helper used by `type`,
`enter`, `pipe`, and `prompt split`.  It first tries a running
`deeppeek daemon` via `POST /internal/relay` on the daemon's
extension port (default 8767 — matching `daemon/cli.py`'s
`--extension-port` default).  If the daemon is up but has no
extension connection on that port, or if it's not running at all,
it falls back to opening a local `ExportServer` on the CLI port
(default 8765).

Why 8767 and not 8765: the daemon deliberately binds its own port
so the CLI can run side-by-side with it.  The extension dials both
(`DSX_PORTS = [8765, 8767]` in `background.js`), which means a CLI
command that falls back to 8765 always finds a live SSE connection,
and a CLI command that finds the daemon up on 8767 rides its
already-connected SSE — saving `ExportServer.start()` plus the
extension-reconnect wait on the CLI side.
"""

import json as _json
import queue
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

from ..capture.server import ExportServer


# Daemon's default extension-SSE port.  Must match
# `daemon/cli.py`'s `--extension-port` default.  The CLI's own
# default extension port is 8765 (see `composer_session`).
DEFAULT_DAEMON_PORT = 8767


@contextmanager
def composer_session(port: int = 8765, timeout: float = 30.0):
    """Open ExportServer, wait for the extension to connect.

    Yields the connected server, or None if the extension never
    connected or the port was in use.  Always cleans up on exit.
    """
    try:
        server = ExportServer(port=port)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"error: port {port} is already in use — is another "
                  f"`deeppeek` command running?", file=sys.stderr)
            print("  stop it first (Ctrl+C the other command), then retry.",
                  file=sys.stderr)
            yield None
            return
        raise

    try:
        print(f"Waiting for extension on "
              f"http://127.0.0.1:{port}/events "
              f"(up to {timeout:.0f}s)...", file=sys.stderr)
        if not server.wait_for_extension(timeout=timeout):
            print("error: no extension connected within timeout.",
                  file=sys.stderr)
            print("  make sure Zen is open, the extension is loaded, and a "
                  "deepseek.com tab is active.", file=sys.stderr)
            yield None
            return
        yield server
    finally:
        server.stop()


def broadcast_and_wait(
    server: ExportServer,
    event_name: str,
    payload: dict,
    *,
    timeout: float,
    ack_name: str,
) -> tuple[int, dict]:
    """Broadcast an SSE event, wait for its ack.

    Returns (exit_code, result_dict):
      0 — success (result.success is True)
      1 — infrastructure failure (timeout, malformed ack, batch_id
          mismatch)
      2 — command failure (extension reported success:false)
    """
    batch_id = uuid.uuid4().hex[:12]
    full_payload = {**payload, "batch_id": batch_id}
    server._broadcast(event_name, full_payload)

    try:
        ack = server.wait_for_result(timeout=timeout)
    except queue.Empty:
        print("error: extension did not acknowledge within timeout.",
              file=sys.stderr)
        return 1, {}

    if ack.get("__ack") != ack_name:
        print(f"error: unexpected response: {ack!r}", file=sys.stderr)
        return 1, {}
    if ack.get("batch_id") != batch_id:
        print(f"error: response batch_id mismatch "
              f"(got {ack.get('batch_id')}, expected {batch_id})",
              file=sys.stderr)
        return 1, {}

    result = ack.get("result") or {}
    if result.get("success"):
        return 0, result
    return 2, result


def broadcast_many(server: ExportServer, specs: list[dict],
                   *, timeout: float) -> dict:
    """Broadcast N events back-to-back on one connection, collect N acks.

    `specs` is a list of dicts:
        {"tag": str,           # caller-facing label for the result
         "event": str,         # SSE event name (attach / type / enter)
         "payload": dict,      # event body (batch_id will be added)
         "ack_name": str}      # expected __ack value

    Each broadcast gets its own batch_id.  Acks are matched back by
    batch_id, so the return value is keyed by `tag` and is independent
    of arrival order.  If the same event name is fired twice, tags
    keep the results distinct.

    Returns: dict tag -> {"exit_code": int, "result": dict}
      exit_code as in `broadcast_and_wait`.
    """
    pending: dict[str, dict] = {}
    for spec in specs:
        batch_id = uuid.uuid4().hex[:12]
        full_payload = {**spec.get("payload", {}), "batch_id": batch_id}
        server._broadcast(spec["event"], full_payload)
        pending[batch_id] = spec

    results: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ack = server.wait_for_result(timeout=remaining)
        except queue.Empty:
            break
        batch_id = ack.get("batch_id")
        spec = pending.pop(batch_id, None)
        if spec is None:
            continue  # unknown ack — ignore
        tag = spec["tag"]
        if ack.get("__ack") != spec["ack_name"]:
            results[tag] = {
                "exit_code": 1,
                "result": {"error": f"unexpected ack: {ack!r}"},
            }
            continue
        result = ack.get("result") or {}
        if result.get("success"):
            results[tag] = {"exit_code": 0, "result": result}
        else:
            results[tag] = {"exit_code": 2, "result": result}

    # Anything still pending timed out.
    for batch_id, spec in pending.items():
        results[spec["tag"]] = {
            "exit_code": 1,
            "result": {"error": "timeout waiting for ack"},
        }
    return results


def _try_daemon_relay(
    event_name: str,
    payload: dict,
    *,
    timeout: float,
    ack_name: str,
    port: int,
):
    """POST one event to a running daemon's /internal/relay.

    Returns:
      None           — the caller must fall back to a local
                       ExportServer.  Two cases:
                         · no daemon on `port` (ConnectionRefused)
                         · daemon is up but its SSE port has no
                           connected extension client — the CLI
                           should try its own port (8765), which
                           the extension also dials.
      (rc, result)   — daemon answered with a final result.
                       rc uses broadcast_and_wait semantics.
    """
    batch_id = uuid.uuid4().hex[:12]
    full_payload = {**payload, "batch_id": batch_id}
    body = _json.dumps({
        "event": event_name,
        "payload": full_payload,
        "ack_name": ack_name,
        "timeout": timeout,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/internal/relay",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            data = _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        print(f"error: daemon relay HTTP {e.code}: {text}",
              file=sys.stderr)
        return 1, {}
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ConnectionRefusedError):
            return None
        print(f"error: daemon relay failed: {e}", file=sys.stderr)
        return 1, {}
    except (OSError, ValueError) as e:
        print(f"error: daemon relay failed: {e}", file=sys.stderr)
        return 1, {}

    if not data.get("ok"):
        err = data.get("error") or ""
        # The daemon answered but its SSE port has no extension
        # client.  Legitimate reason to fall back: the extension's
        # retryAfter can be non-zero on 8767 while its 8765 stream
        # is still live, so opening a local ExportServer on 8765 is
        # exactly the right move.  If both ports are dead, the
        # fallback's own wait_for_extension() will time out and
        # surface a clear "no extension connected" error.
        if "no extension connected" in err:
            return None
        print(f"error: daemon relay: {err}", file=sys.stderr)
        return 1, {}

    ack = data.get("ack") or {}
    if ack.get("batch_id") != batch_id:
        print(f"error: response batch_id mismatch "
              f"(got {ack.get('batch_id')}, expected {batch_id})",
              file=sys.stderr)
        return 1, {}
    print(f"Routed via daemon on port {port}.", file=sys.stderr)
    result = ack.get("result") or {}
    if result.get("success"):
        return 0, result
    return 2, result


def submit_request(
    event_name: str,
    payload: dict,
    *,
    timeout: float,
    ack_name: str,
    port: int = 8765,
    daemon_port: int = DEFAULT_DAEMON_PORT,
) -> tuple[int, dict]:
    """Send one event to the extension.

    Tries a running `deeppeek daemon` on `daemon_port` (default 8767,
    matching `daemon/cli.py`'s `--extension-port` default).  Falls
    back to opening a local ExportServer on `port` (default 8765) —
    the CLI path, and what keeps the CLI working when no daemon is
    running.

    Return value matches `broadcast_and_wait`.
    """
    relay = _try_daemon_relay(
        event_name, payload,
        timeout=timeout, ack_name=ack_name, port=daemon_port,
    )
    if relay is not None:
        return relay
    with composer_session(port=port, timeout=timeout) as server:
        if server is None:
            return 1, {}
        return broadcast_and_wait(
            server, event_name, payload,
            timeout=timeout, ack_name=ack_name,
        )
