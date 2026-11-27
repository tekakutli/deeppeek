"""BrowserBridge — orchestrates one chat completion through the extension."""
import os
import queue
import sys
import threading
import time
import uuid

from .serialize import serialize_messages


TAB_OPEN_TIMEOUT = 30.0
SUBMIT_TIMEOUT = 45.0
WATCH_START_TIMEOUT = 10.0
# Default wait after `open-tab` ack before submitting.  The old 2.0s
# value was chosen to mask slow tab loads; the submit path already
# retries 3× with a 2s backoff, so the initial wait only needs to
# cover the common "tab renders the composer in <500ms" case.  Slow
# loads are caught by the retry loop.
TAB_WAIT_DEFAULT = 0.5
WATCH_MAX_WAIT = 600.0

# How long the extension waits for ANY first content (thinking or
# answer) before declaring submit failure.  The historical 120s
# default was tuned for interactive DeepSeek use; reasoning models
# routinely think for 3-5 minutes on hard prompts.  10 min default,
# override with DEEPPEEK_FIRST_CONTENT_TIMEOUT_MS (milliseconds).
FIRST_CONTENT_TIMEOUT_MS = int(
    os.environ.get("DEEPPEEK_FIRST_CONTENT_TIMEOUT_MS", "600000")
)
# Hard cap on a single watch cycle (thinking + answer).  15 min
# default, override with DEEPPEEK_MAX_DURATION_MS (milliseconds).
DEFAULT_MAX_DURATION_MS = int(
    os.environ.get("DEEPPEEK_MAX_DURATION_MS", "900000")
)


class BridgeError(Exception):
    """Raised when the browser bridge cannot fulfill a request."""


class BrowserBridge:
    """Serialize + submit + stream one chat completion via the extension.

    One lock serializes requests against the single browser tab.
    Concurrent requests from aider (main chat + summarizer) queue.

    Tab selection is per-request: the constructor sets *defaults*,
    and each `stream()` / `complete()` call may pass a `tab_options`
    dict that overrides them.  Recognised keys:

        mode         "new" | "reuse_active" | "reuse_in_container" | "by_id"
        container    str | None      (mode == "new" / "reuse_in_container")
        container_id str | None      (mode == "new" / "reuse_in_container")
        tab_id       int | None      (mode == "by_id")

    `mode == "new"` opens a fresh tab (in `container` / `container_id`
    when given).  `reuse_active` targets whichever tab is focused
    (no tab id is sent).  `reuse_in_container` asks the extension to
    find an existing tab in the container and reuse it, opening a
    new one only if none exists — the extension honours the
    `reuse: "if_exists"` field of the `open-tab` payload.
    `by_id` skips `open-tab` entirely and pins to the given tab id.

    `get_active_tab()` is a read-only query used by
    `deeppeek daemon --which-tab` to discover a tab id to pass to
    `--reuse-tab-by`.  It uses a non-blocking lock so it fails fast
    when a completion is in flight (the two would otherwise race on
    the shared result queue).

    Server-side error banners (quota exhaustion, rate limits, model
    unavailable, context length) are surfaced as BridgeError by
    `_watch()` when the extension emits `reason: "error_banner"` with
    a structured `error` payload.  This prevents the daemon from
    silently returning the previous turn's assistant message as if
    it were the current reply — the browser-side error detector
    (injected-error-patterns.js) runs before any content extraction.
    """

    def __init__(
        self,
        server,
        *,
        default_mode: str = "new",
        default_container: str | None = None,
        default_container_id: str | None = None,
        default_tab_id: int | None = None,
        tab_wait: float = TAB_WAIT_DEFAULT,
        poll_interval_ms: int = 300,
        settle_ticks: int = 5,
        max_duration_ms: int = DEFAULT_MAX_DURATION_MS,
        first_content_timeout_ms: int = FIRST_CONTENT_TIMEOUT_MS,
    ):
        if default_mode not in (
                "new", "reuse_active", "reuse_in_container", "by_id"):
            raise ValueError(f"unknown default_mode: {default_mode!r}")
        self.server = server
        self.default_mode = default_mode
        self.default_container = default_container
        self.default_container_id = default_container_id
        self.default_tab_id = default_tab_id
        self.tab_wait = tab_wait
        self.poll_interval_ms = poll_interval_ms
        self.settle_ticks = settle_ticks
        self.max_duration_ms = max_duration_ms
        self.first_content_timeout_ms = first_content_timeout_ms
        self._lock = threading.Lock()
        # Set by _prepare_tab; passed to submit/watch so they pin to
        # the same tab even if the user switches windows mid-request.
        self.tab_id: int | None = None

    # ---- public ----------------------------------------------------

    def complete(self, messages: list[dict], model: str,
                 tab_options: dict | None = None) -> str:
        """Blocking, non-streaming completion."""
        return "".join(self.stream(messages, model,
                                   tab_options=tab_options))

    def stream(self, messages: list[dict], model: str,
               tab_options: dict | None = None):
        """Generator of content deltas."""
        with self._lock:
            self._drain_queue()
            # Reset per-request state: a previous request's tab_id
            # must not leak into this one.
            self.tab_id = None
            try:
                self._prepare_tab(tab_options)
                prompt = serialize_messages(messages)
                self._submit(prompt)
                yield from self._watch()
            except BridgeError:
                raise
            except Exception as e:
                raise BridgeError(f"bridge failure: {e}") from e

    def get_active_tab(self) -> dict:
        """Ask the extension which tab is active.  Read-only.

        Returns the extension's reply verbatim:

            {"success": True,
             "tab_id": int,
             "url": str,
             "title": str | None,
             "cookie_store_id": str | None,
             "source": "active" | "only-supported"}

        or, on failure:

            {"success": False,
             "reason": str,
             "candidates": [{"tab_id": ..., "url": ..., "active": ...}]}

        Uses a non-blocking lock so ``--which-tab`` fails fast instead
        of queueing behind a long in-flight completion; the same
        result queue is shared with ``stream()``, so concurrency here
        would otherwise race the delta reader.
        """
        if not self._lock.acquire(blocking=False):
            raise BridgeError(
                "daemon is busy processing a chat completion; "
                "retry --which-tab once it's idle")
        try:
            return self._call("get-active-tab", {},
                              ack="get-active-tab", timeout=10.0)
        finally:
            self._lock.release()

    # ---- internals -------------------------------------------------

    def _drain_queue(self) -> None:
        """Discard any queued acks from a previous (abandoned) request."""
        while True:
            try:
                self.server.result_queue.get_nowait()
            except queue.Empty:
                break

    def _resolve_tab_options(self, request_opts: dict | None) -> dict:
        """Merge request-level tab options over daemon defaults."""
        opts = {
            "mode": self.default_mode,
            "container": self.default_container,
            "container_id": self.default_container_id,
            "tab_id": self.default_tab_id,
        }
        if request_opts:
            for key in ("mode", "container", "container_id", "tab_id"):
                if key in request_opts and request_opts[key] is not None:
                    opts[key] = request_opts[key]
        if opts["mode"] not in (
                "new", "reuse_active", "reuse_in_container", "by_id"):
            raise BridgeError(f"unknown tab mode: {opts['mode']!r}")
        return opts

    def _prepare_tab(self, request_opts: dict | None = None) -> None:
        opts = self._resolve_tab_options(request_opts)
        mode = opts["mode"]

        if mode == "reuse_active":
            print("[daemon] reusing active tab", file=sys.stderr)
            return

        if mode == "by_id":
            if opts["tab_id"] is None:
                raise BridgeError(
                    "tab mode 'by_id' requires a tab_id")
            self.tab_id = int(opts["tab_id"])
            print(f"[daemon] targeting tab {self.tab_id}",
                  file=sys.stderr)
            return

        # mode is "new" or "reuse_in_container"
        payload: dict = {"url": "https://chat.deepseek.com/"}
        container_desc = ""
        if opts.get("container"):
            payload["container"] = opts["container"]
            container_desc = f" in container {opts['container']!r}"
        elif opts.get("container_id"):
            payload["container_id"] = opts["container_id"]
            container_desc = (
                f" in container-id {opts['container_id']!r}")

        if mode == "reuse_in_container":
            if not container_desc:
                raise BridgeError(
                    "tab mode 'reuse_in_container' requires "
                    "container or container_id")
            payload["reuse"] = "if_exists"
            print(
                f"[daemon] find-or-open DeepSeek tab{container_desc}",
                file=sys.stderr,
            )
        else:
            print(
                f"[daemon] opening new DeepSeek tab{container_desc}",
                file=sys.stderr,
            )

        result = self._call(
            "open-tab",
            payload,
            ack="open-tab",
            timeout=TAB_OPEN_TIMEOUT,
        )
        # The extension acks with `success: false` when, e.g., the
        # requested container name doesn't resolve or matches more
        # than one container.
        if not result.get("success"):
            raise BridgeError(
                f"open-tab failed: "
                f"{result.get('reason') or '(no reason)'}"
            )
        self.tab_id = result.get("tab_id")
        cs_id = result.get("cookie_store_id")
        reused = bool(result.get("reused"))
        detail = f"tab {self.tab_id} -> {result.get('url')}"
        if cs_id:
            detail += f"  (container: {cs_id})"
        if reused:
            detail += "  [reused]"
        print(f"[daemon] opened {detail}", file=sys.stderr)
        if self.tab_wait > 0:
            time.sleep(self.tab_wait)

    def _submit(self, prompt: str) -> None:
        print(f"[daemon] submitting {len(prompt):,} chars", file=sys.stderr)
        payload = {"text": prompt}
        if self.tab_id is not None:
            payload["tab_id"] = self.tab_id
        last_error: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2.0)
                print(
                    f"[daemon] submit retry {attempt}/2", file=sys.stderr,
                )
            try:
                result = self._call(
                    "submit", payload,
                    ack="submit", timeout=SUBMIT_TIMEOUT,
                )
            except BridgeError as e:
                last_error = e
                continue
            if result.get("success"):
                return
            last_error = BridgeError(
                f"submit failed: {result.get('reason') or '(no reason)'}"
            )
        raise BridgeError(f"submit failed after retries: {last_error}")

    def _watch(self):
        batch_id = uuid.uuid4().hex[:12]
        print("[daemon] starting watch", file=sys.stderr)
        watch_payload = {
            "batch_id": batch_id,
            "include_forked": False,
            "poll_interval_ms": self.poll_interval_ms,
            "settle_ticks": self.settle_ticks,
            "max_duration_ms": self.max_duration_ms,
            "first_content_timeout_ms": self.first_content_timeout_ms,
        }
        if self.tab_id is not None:
            watch_payload["tab_id"] = self.tab_id
        self.server._broadcast("watch", watch_payload)

        # Wait for the watch start ack.
        deadline = time.monotonic() + WATCH_START_TIMEOUT
        started = False
        while time.monotonic() < deadline:
            try:
                msg = self.server.wait_for_result(timeout=0.5)
            except queue.Empty:
                continue
            if (msg.get("__ack") == "watch"
                    and msg.get("batch_id") == batch_id):
                started = True
                break
        if not started:
            raise BridgeError("watch start ack not received")

        # Consume deltas until watch-done.
        total = 0
        watch_start = time.monotonic()
        deadline = watch_start + WATCH_MAX_WAIT
        last_heartbeat = watch_start
        while time.monotonic() < deadline:
            try:
                msg = self.server.wait_for_result(timeout=0.5)
            except queue.Empty:
                now = time.monotonic()
                if now - last_heartbeat >= 10.0:
                    print(
                        f"[daemon] still waiting for content "
                        f"({now - watch_start:.0f}s elapsed, "
                        f"{total:,} chars so far) — browser is "
                        f"generating (thought phase)",
                        file=sys.stderr,
                    )
                    last_heartbeat = now
                continue
            if msg.get("batch_id") != batch_id:
                continue
            ack = msg.get("__ack")
            if ack == "watch-delta":
                delta = msg.get("delta", "")
                if delta:
                    total += len(delta)
                    last_heartbeat = time.monotonic()
                    yield delta
            elif ack == "watch-rewrite":
                content = msg.get("content", "")
                print(
                    f"[daemon] watch-rewrite ({len(content)} chars) — "
                    f"passing through",
                    file=sys.stderr,
                )
                total += len(content)
                last_heartbeat = time.monotonic()
                yield content
            elif ack == "watch-done":
                reason = msg.get("reason")

                # NEW: structured error from the extension's
                # error-banner detector (injected-error-patterns.js).
                # Checked BEFORE the total==0 branch: if stale content
                # was emitted before the banner appeared, we still
                # want to surface the error (an error always wins over
                # a partial reply).
                error = msg.get("error")
                if error:
                    err_cat = error.get("category", "unknown")
                    err_text = error.get("text", "(no text)")
                    err_hint = error.get("hint") or ""
                    err_msg = (
                        f"DeepSeek error [{err_cat}]: {err_text}"
                    )
                    if err_hint:
                        err_msg += f" — {err_hint}"
                    if total > 0:
                        err_msg += (
                            f" (discarding {total:,} chars of "
                            f"stale content already emitted)"
                        )
                    raise BridgeError(err_msg)

                if total == 0:
                    if reason == "submit_failed":
                        raise BridgeError(
                            "submit appears to have been blocked — no "
                            "new message appeared in the chat within "
                            "10s of pressing Enter. Common causes:\n"
                            "  · a userscript intercepted the Enter "
                            "keypress (e.g. Anti-recall)\n"
                            "  · another browser extension is "
                            "listening on the composer\n"
                            "  · the DeepSeek tab is not responsive\n"
                            "Try disabling interfering userscripts, or "
                            "reloading the DeepSeek tab (F5), then "
                            "retry."
                        )
                    if reason == "no_first_content":
                        raise BridgeError(
                            "model produced no output within the "
                            "first-content timeout — the prompt may "
                            "not have been submitted, or the web UI "
                            "is not streaming to IndexedDB. Check the "
                            "tab.  (Raise "
                            "DEEPPEEK_FIRST_CONTENT_TIMEOUT_MS if the "
                            "thinking phase is legitimately long.)"
                        )
                    if reason == "no_deepseek_tab":
                        raise BridgeError(
                            "the DeepSeek tab closed or navigated away "
                            "mid-watch"
                        )
                    if reason == "poll_error":
                        raise BridgeError(
                            "watch poll errored — see browser console"
                        )
                    print(
                        f"[daemon] WARNING: watch done with 0 chars "
                        f"(reason={reason!r})",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[daemon] watch done ({total:,} chars, "
                        f"reason={reason!r})",
                        file=sys.stderr,
                    )
                return

        raise BridgeError(
            f"watch timed out after {WATCH_MAX_WAIT:.0f}s "
            f"(received {total:,} chars)"
        )

    def _call(
        self,
        event: str,
        payload: dict,
        *,
        ack: str,
        timeout: float,
    ) -> dict:
        batch_id = uuid.uuid4().hex[:12]
        self.server._broadcast(event, {**payload, "batch_id": batch_id})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.server.wait_for_result(timeout=0.5)
            except queue.Empty:
                continue
            if (msg.get("__ack") == ack
                    and msg.get("batch_id") == batch_id):
                return msg.get("result") or {}
        raise BridgeError(f"{ack} ack not received within {timeout}s")