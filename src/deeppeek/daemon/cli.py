"""``deeppeek daemon`` — argparse registration + run."""
import argparse
import json
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request

from ..transport.server import ExportServer
from .bridge import (
    BrowserBridge,
    DEFAULT_MAX_DURATION_MS,
    FIRST_CONTENT_TIMEOUT_MS,
)
from .server import serve


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "daemon",
        help="Run an OpenAI-compatible model server backed by the "
             "browser extension (aider can dial into it)",
    )
    p.add_argument(
        "--port", type=int, default=8766,
        help="OpenAI-compatible HTTP port (default: 8766).  Also "
             "used by --shutdown and --which-tab to locate a running "
             "daemon.",
    )
    p.add_argument(
        "--extension-port", type=int, default=8767,
        help="extension SSE port (default: 8767).  Kept distinct from "
             "the CLI's 8765 so `deeppeek daemon` and interactive "
             "commands (`type`/`enter`/`pipe`/`capture`) can run "
             "concurrently.  Also used by --shutdown and --which-tab "
             "to locate a running daemon.",
    )
    p.add_argument(
        "--print-output", action="store_true",
        help="echo every text delta the daemon streams back to the "
             "client (aider) to stdout.",
    )
    p.add_argument(
        "--shutdown", action="store_true",
        help="stop a running `deeppeek daemon` instance and exit.  "
             "Uses the PID files written by the running daemon (in "
             "XDG_RUNTIME_DIR, or /tmp if unset) and sends SIGTERM "
             "so it follows the same clean teardown path as Ctrl+C.  "
             "Escalates to SIGKILL if the process does not exit "
             "within 5 s.  Exits 0 whether or not a daemon was "
             "running; exits 1 only on an actual error (e.g. "
             "permission denied while signalling).",
    )
    p.add_argument(
        "--which-tab", action="store_true",
        help="print the id of the currently active DeepSeek tab and "
             "exit.  Talks to a running daemon if one is up; "
             "otherwise spins up an ephemeral instance (requires the "
             "extension to reconnect).  Bare id on stdout, details "
             "on stderr — suitable for "
             "`TAB=$(deeppeek daemon --which-tab)` and then "
             "`deeppeek daemon --reuse-tab-by \"$TAB\"`.  Cannot be "
             "combined with tab-selection flags.",
    )

    # ---- tab selection (daemon-wide defaults) ----------------------
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--reuse-tab", action="store_true",
        help="reuse the currently active DeepSeek tab instead of "
             "opening a new one per request.  WARNING: the model "
             "sees the tab's prior conversation as context.",
    )
    mode_group.add_argument(
        "--reuse-tab-in", default=None, metavar="NAME",
        help="reuse an existing tab in the named container, opening "
             "one only if none exists.  Requires extension support "
             "for `open-tab { reuse: 'if_exists' }`.",
    )
    mode_group.add_argument(
        "--reuse-tab-by", type=int, default=None, metavar="TAB_ID",
        help="pin every request to this exact browser tab id (as "
             "reported by `deeppeek daemon --which-tab`).  Skips "
             "open-tab entirely, so it works across container lines "
             "— the tab's container is whatever it already is.  "
             "Mutually exclusive with --reuse-tab, --reuse-tab-in, "
             "--container, and --container-id.  The id is only "
             "valid while that tab remains open.",
    )

    p.add_argument(
        "--container", default=None, metavar="NAME",
        help="open new DeepSeek tabs inside the Firefox/Zen "
             "Container Tab whose display name matches NAME.  "
             "Mutually exclusive with --container-id, --reuse-tab, "
             "--reuse-tab-in, and --reuse-tab-by.",
    )
    p.add_argument(
        "--container-id", default=None, metavar="COOKIE_STORE_ID",
        help="open new DeepSeek tabs inside the container with this "
             "raw cookieStoreId (e.g. 'firefox-container-3').  "
             "Mutually exclusive with --container, --reuse-tab, "
             "--reuse-tab-in, and --reuse-tab-by.",
    )

    p.add_argument(
        "--tab-wait", type=float, default=0.5,
        help="seconds to wait after opening a new tab before "
             "submitting (default: 0.5).",
    )
    p.add_argument(
        "--connect-timeout", type=float, default=30.0,
        help="seconds to wait for the extension to connect "
             "(default: 30)",
    )
    p.add_argument(
        "--first-content-timeout", type=float, default=None,
        metavar="SECONDS",
        help="how long the extension will wait for any first content "
             "(thinking or answer) before reporting submit failure. "
             f"Default: {FIRST_CONTENT_TIMEOUT_MS // 1000} s (10 min).",
    )
    p.add_argument(
        "--max-duration", type=float, default=None,
        metavar="SECONDS",
        help="hard cap on a single watch cycle (thinking + answer). "
             f"Default: {DEFAULT_MAX_DURATION_MS // 1000} s (15 min).",
    )
    p.set_defaults(func=run)


def _validate_tab_args(args) -> int | None:
    """Return 0/1/3 on argument error, None on success.

    3 matches the argument-error convention already used by
    `prompt new`, `capture`, and `recover`.
    """
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
    if args.reuse_tab_in is not None and not args.reuse_tab_in.strip():
        print("error: --reuse-tab-in is empty (whitespace-only)",
              file=sys.stderr)
        return 3
    if ((args.container is not None or args.container_id is not None)
            and args.reuse_tab):
        print("error: --container/--container-id cannot be combined "
              "with --reuse-tab (reuse targets the active tab, "
              "whose container is whatever it already is)",
              file=sys.stderr)
        return 3
    if ((args.container is not None or args.container_id is not None)
            and args.reuse_tab_in is not None):
        print("error: --reuse-tab-in and --container/--container-id "
              "are mutually exclusive", file=sys.stderr)
        return 3
    # argparse already excludes --reuse-tab-by vs --reuse-tab /
    # --reuse-tab-in via mode_group; it does NOT know about the
    # container flags, so enforce that here.
    if args.reuse_tab_by is not None and (
            args.container is not None or args.container_id is not None):
        print("error: --reuse-tab-by cannot be combined with "
              "--container/--container-id (the target tab already "
              "has its own container)", file=sys.stderr)
        return 3
    return None


def _default_mode_and_scope(
        args) -> tuple[str, str | None, str | None, int | None]:
    """Translate CLI defaults into the (mode, container,
    container_id, tab_id) tuple that BrowserBridge wants."""
    if args.reuse_tab_by is not None:
        return "by_id", None, None, args.reuse_tab_by
    if args.reuse_tab:
        return "reuse_active", None, None, None
    if args.reuse_tab_in is not None:
        return "reuse_in_container", args.reuse_tab_in, None, None
    if args.container is not None:
        return "new", args.container, None, None
    if args.container_id is not None:
        return "new", None, args.container_id, None
    return "new", None, None, None


# ---- PID file plumbing (used by --shutdown) -------------------------

_SHUTDOWN_TIMEOUT_S = 5.0


def _pidfile_path(port: int, kind: str) -> str:
    """Path to the PID file for the given port.

    ``kind`` is 'openai' or 'extension'.  We key on both ports because
    the daemon binds both, and either one being occupied is what makes
    starting a second daemon fail — so --shutdown must be able to find
    a running daemon via either PID file.

    Prefers XDG_RUNTIME_DIR (correct place for per-user runtime state;
    auto-cleaned at logout), falling back to the system temp dir.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, f"deeppeek-daemon-{kind}-{port}.pid")


def _write_pidfile(path: str) -> None:
    """Write our PID atomically so a concurrent reader never sees a
    half-written file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            f.write(f"{os.getpid()}\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"[daemon] warning: cannot write PID file {path}: {e}",
              file=sys.stderr)
        _unlink_quiet(tmp)


def _read_pid(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _remove_pidfile_if_ours(path: str) -> None:
    """Only remove the PID file if it still belongs to us — protects
    against a race where two daemons started concurrently and one is
    now cleaning up the other's file."""
    if _read_pid(path) == os.getpid():
        _unlink_quiet(path)


def _pid_alive(pid: int) -> bool:
    """Return True if the process is running (and not a zombie).

    On Linux we consult /proc/<pid>/status to reject zombies — a
    killed daemon can linger in the process table until its parent
    reaps it, and `kill(pid, 0)` still succeeds for zombies.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists but owned by another user
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    fields = line.split()
                    if len(fields) >= 2 and fields[1] == "Z":
                        return False
                    return True
    except (FileNotFoundError, PermissionError):
        pass
    return True


def _is_deeppeek_daemon(pid: int) -> bool:
    """Best-effort check that ``pid`` is a deeppeek daemon.

    Guards against killing a recycled PID.  Only implemented on Linux
    (via /proc/<pid>/cmdline); returns True elsewhere since PID reuse
    within the shutdown window is extremely unlikely and we'd rather
    err on the side of acting.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode(
                errors="replace")
    except (FileNotFoundError, PermissionError):
        return True
    return "deeppeek" in cmdline and "daemon" in cmdline


def _terminate(pid: int,
               timeout: float = _SHUTDOWN_TIMEOUT_S) -> bool:
    """Ask the process to exit, escalating to SIGKILL on timeout.

    SIGTERM is caught by the daemon's installed handler, which raises
    KeyboardInterrupt so the finally blocks (pidfile cleanup,
    ExportServer.stop) run — same path as Ctrl+C.  Returns True if the
    process is gone, False if it survived SIGKILL.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError as e:
        print(f"[shutdown] cannot signal pid {pid}: {e}",
              file=sys.stderr)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)

    print(f"[shutdown] pid {pid} did not exit after SIGTERM; "
          f"sending SIGKILL", file=sys.stderr)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError as e:
        print(f"[shutdown] cannot SIGKILL pid {pid}: {e}",
              file=sys.stderr)
        return False
    time.sleep(0.2)
    return not _pid_alive(pid)


def _shutdown_existing(port: int, extension_port: int) -> int:
    """Find and stop any daemon running on the given ports."""
    pidfiles = [
        _pidfile_path(port, "openai"),
        _pidfile_path(extension_port, "extension"),
    ]
    seen_pids: set[int] = set()
    stopped_any = False
    failed = False

    for path in pidfiles:
        pid = _read_pid(path)
        if pid is None:
            continue
        if pid in seen_pids:
            # Both PID files normally point at the same process; we
            # don't need to signal it twice.
            _unlink_quiet(path)
            continue
        seen_pids.add(pid)

        if not _pid_alive(pid):
            print(f"[shutdown] stale PID file {path} (pid {pid} not "
                  f"running); removing", file=sys.stderr)
            _unlink_quiet(path)
            continue

        if not _is_deeppeek_daemon(pid):
            print(f"[shutdown] PID {pid} from {path} does not look "
                  f"like a deeppeek daemon; removing stale PID file",
                  file=sys.stderr)
            _unlink_quiet(path)
            continue

        print(f"[shutdown] stopping daemon pid {pid}...",
              file=sys.stderr)
        if _terminate(pid):
            print(f"[shutdown] pid {pid} stopped.", file=sys.stderr)
            stopped_any = True
            _unlink_quiet(path)
        else:
            print(f"[shutdown] failed to stop pid {pid}",
                  file=sys.stderr)
            failed = True

    if failed:
        return 1
    if not stopped_any:
        print(f"[shutdown] no running daemon found for port {port} "
              f"or extension port {extension_port}", file=sys.stderr)
    return 0


def _install_sigterm_handler() -> None:
    """Make SIGTERM behave like Ctrl+C.

    Without this, the default SIGTERM action terminates the process
    without running finally blocks, so the daemon's PID files and the
    ExportServer would not be cleaned up.  --shutdown relies on this
    handler to reach the same teardown path an interactive interrupt
    would.
    """
    def _handler(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt()
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        # Not in the main thread, or the platform lacks SIGTERM.
        # Non-fatal: --shutdown will fall back to SIGKILL.
        pass


# ---- --which-tab ----------------------------------------------------

def _report_which_tab(result: dict) -> int:
    """Render the extension's get-active-tab reply.

    Bare tab_id on stdout so `TAB=$(deeppeek daemon --which-tab)`
    works; everything else goes to stderr.
    """
    tab_id = result.get("tab_id")
    if tab_id is None:
        print(
            f"[which-tab] "
            f"{result.get('reason') or 'no active DeepSeek tab'}",
            file=sys.stderr,
        )
        candidates = result.get("candidates") or []
        if candidates:
            print(f"[which-tab] {len(candidates)} candidate(s):",
                  file=sys.stderr)
            for c in candidates:
                marker = " (active)" if c.get("active") else ""
                print(
                    f"  tab_id={c.get('tab_id')} "
                    f"container={c.get('cookie_store_id') or '-'} "
                    f"url={c.get('url') or '?'}{marker}",
                    file=sys.stderr,
                )
        return 1
    print(tab_id)
    print(
        f"[which-tab] active tab: id={tab_id} "
        f"container={result.get('cookie_store_id') or '-'} "
        f"url={result.get('url') or '?'}",
        file=sys.stderr,
    )
    return 0


def _which_tab(args) -> int:
    """Query a running daemon over HTTP; fall back to an ephemeral
    probe if nothing is listening on --port."""
    url = f"http://127.0.0.1:{args.port}/deeppeek/active-tab"
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
            msg = (body.get("error") or {}).get("message", str(e))
        except Exception:
            msg = str(e)
        print(f"[which-tab] daemon error: {msg}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, ConnectionRefusedError,
            ConnectionResetError, OSError):
        return _ephemeral_which_tab(args)
    except json.JSONDecodeError as e:
        print(f"[which-tab] daemon returned malformed reply: {e}",
              file=sys.stderr)
        return 1
    return _report_which_tab(data)


def _ephemeral_which_tab(args) -> int:
    """Spin up a throwaway ExportServer, wait for the extension,
    query, print, exit.  Only useful when no daemon is bound on the
    extension port — otherwise the extension's single SSE channel is
    already claimed."""
    try:
        ext = ExportServer(port=args.extension_port)
        ext.start()
    except OSError as e:
        print(
            f"error: cannot bind extension port "
            f"{args.extension_port}: {e}",
            file=sys.stderr,
        )
        print(
            "  a daemon is probably bound but not responding on the "
            "HTTP port; try `deeppeek daemon --shutdown` first.",
            file=sys.stderr,
        )
        return 1

    try:
        print(
            f"[which-tab] no running daemon on port {args.port}; "
            f"starting ephemeral probe...",
            file=sys.stderr,
        )
        print(
            f"[which-tab] waiting for extension on port "
            f"{args.extension_port} (up to "
            f"{args.connect_timeout:.0f}s)...",
            file=sys.stderr,
        )
        if not ext.wait_for_extension(timeout=args.connect_timeout):
            print(
                "error: no extension connected; the browser may be "
                "attached to another daemon, or no supported tab is "
                "open",
                file=sys.stderr,
            )
            return 1
        # Throwaway bridge; only get_active_tab is used, so the
        # tab-selection defaults are irrelevant.
        bridge = BrowserBridge(ext)
        try:
            result = bridge.get_active_tab()
        except Exception as e:
            print(f"[which-tab] query failed: {e}", file=sys.stderr)
            return 1
    finally:
        ext.stop()

    return _report_which_tab(result)


def run(args: argparse.Namespace) -> int:
    if args.shutdown:
        # --shutdown short-circuits before tab validation: the tab
        # flags are irrelevant when we're not opening any tabs.
        return _shutdown_existing(args.port, args.extension_port)

    if args.which_tab:
        # --which-tab is a standalone query; tab-selection flags make
        # no sense here and indicate a scripting error.
        if (args.reuse_tab or args.reuse_tab_in is not None
                or args.reuse_tab_by is not None
                or args.container is not None
                or args.container_id is not None):
            print(
                "error: --which-tab is a standalone query; it cannot "
                "be combined with tab/container selection flags",
                file=sys.stderr,
            )
            return 3
        return _which_tab(args)

    rc = _validate_tab_args(args)
    if rc is not None:
        return rc

    try:
        ext = ExportServer(port=args.extension_port)
        ext.start()
    except OSError as e:
        print(
            f"error: cannot bind extension port {args.extension_port}: {e}",
            file=sys.stderr,
        )
        print(
            "  another deeppeek command may be running; run "
            "`deeppeek daemon --shutdown` to stop it",
            file=sys.stderr,
        )
        return 1

    # From this point the daemon owns both ports, so record its PID
    # for a future `deeppeek daemon --shutdown`.  Also make SIGTERM
    # follow the clean Ctrl+C path so --shutdown can rely on it.
    _install_sigterm_handler()
    pidfiles = [
        _pidfile_path(args.port, "openai"),
        _pidfile_path(args.extension_port, "extension"),
    ]
    for pf in pidfiles:
        _write_pidfile(pf)

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

        first_content_ms = (
            int(args.first_content_timeout * 1000)
            if args.first_content_timeout is not None
            else FIRST_CONTENT_TIMEOUT_MS
        )
        max_duration_ms = (
            int(args.max_duration * 1000)
            if args.max_duration is not None
            else DEFAULT_MAX_DURATION_MS
        )
        (default_mode, default_container, default_container_id,
         default_tab_id) = _default_mode_and_scope(args)

        bridge = BrowserBridge(
            ext,
            default_mode=default_mode,
            default_container=default_container,
            default_container_id=default_container_id,
            default_tab_id=default_tab_id,
            tab_wait=args.tab_wait,
            first_content_timeout_ms=first_content_ms,
            max_duration_ms=max_duration_ms,
        )

        base = f"http://127.0.0.1:{args.port}/v1"
        print("", file=sys.stderr)
        print(
            f"[daemon] OpenAI-compatible endpoint ready: {base}",
            file=sys.stderr,
        )
        if default_mode == "by_id":
            print(
                f"[daemon] default: pinned to tab id {default_tab_id}",
                file=sys.stderr,
            )
        elif default_mode == "new" and default_container:
            print(
                f"[daemon] default: new tab in container: "
                f"{default_container}",
                file=sys.stderr,
            )
        elif default_mode == "new" and default_container_id:
            print(
                f"[daemon] default: new tab in container-id: "
                f"{default_container_id}",
                file=sys.stderr,
            )
        elif default_mode == "reuse_active":
            print(
                "[daemon] default: reuse active tab",
                file=sys.stderr,
            )
        elif default_mode == "reuse_in_container":
            print(
                f"[daemon] default: reuse-or-open tab in container: "
                f"{default_container}",
                file=sys.stderr,
            )
        if args.print_output:
            print(
                "[daemon] --print-output: model replies will be echoed "
                "to stdout",
                file=sys.stderr,
            )
        print(
            f"[daemon] first-content timeout: "
            f"{first_content_ms / 1000:.0f}s   "
            f"max watch duration: {max_duration_ms / 1000:.0f}s",
            file=sys.stderr,
        )
        print(f"[daemon]   export OPENAI_API_BASE={base}", file=sys.stderr)
        print(f"[daemon]   export OPENAI_API_KEY=dummy", file=sys.stderr)
        print(
            f"[daemon]   aider --model openai/deepseek-web",
            file=sys.stderr,
        )
        print(f"[daemon]   stop with `deeppeek daemon --shutdown`",
              file=sys.stderr)
        print(f"[daemon]   current tab id: `deeppeek daemon --which-tab`",
              file=sys.stderr)
        print("", file=sys.stderr)

        serve(bridge, port=args.port, print_output=args.print_output)
        return 0
    except KeyboardInterrupt:
        # Reached via Ctrl+C, SIGTERM (our handler raises
        # KeyboardInterrupt), or `--shutdown` on another terminal.
        print("\n[daemon] stopping.", file=sys.stderr)
        return 0
    finally:
        # Remove PID files first, then shut down the ExportServer —
        # if the process is killed mid-stop, at least it doesn't
        # leave a stale PID file that --shutdown would have to clean
        # up on its next run.
        for pf in pidfiles:
            _remove_pidfile_if_ours(pf)
        ext.stop()