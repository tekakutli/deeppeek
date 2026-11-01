"""BrowserBridge — orchestrates one chat completion through the extension."""
import queue
import sys
import threading
import time
import uuid

from .serialize import serialize_messages


TAB_OPEN_TIMEOUT = 30.0
SUBMIT_TIMEOUT = 45.0
WATCH_START_TIMEOUT = 10.0
TAB_WAIT_DEFAULT = 2.0
WATCH_MAX_WAIT = 600.0


class BridgeError(Exception):
    """Raised when the browser bridge cannot fulfill a request."""


class BrowserBridge:
    """Serialize + submit + stream one chat completion via the extension.

    One lock serializes requests against the single browser tab.
    Concurrent requests from aider (main chat + summarizer) queue.
    """

    def __init__(
        self,
        server,
        *,
        new_tab: bool = True,
        tab_wait: float = TAB_WAIT_DEFAULT,
        poll_interval_ms: int = 300,
        settle_ticks: int = 5,
        max_duration_ms: int = 180_000,
        container: str | None = None,
        container_id: str | None = None,
    ):
        self.server = server
        self.new_tab = new_tab
        self.tab_wait = tab_wait
        self.poll_interval_ms = poll_interval_ms
        self.settle_ticks = settle_ticks
        self.max_duration_ms = max_duration_ms
        # Container selection for freshly-opened tabs.  Both are
        # None by default (open in the browser's default container).
        # Only `new_tab=True` runs make use of them; with
        # `new_tab=False` (--reuse-tab) the current tab is reused
        # and its container is whatever it already is.
        self.container = container
        self.container_id = container_id
        self._lock = threading.Lock()
        # Set by _prepare_tab; passed to submit/watch so they pin to
        # the same tab even if the user switches windows mid-request.
        self.tab_id: int | None = None

    # ---- public ----------------------------------------------------

    def complete(self, messages: list[dict], model: str) -> str:
        """Blocking, non-streaming completion."""
        return "".join(self.stream(messages, model))

    def stream(self, messages: list[dict], model: str):
        """Generator of content deltas."""
        with self._lock:
            self._drain_queue()
            # Reset per-request state: a previous request's tab_id
            # must not leak into this one (different request, likely
            # different tab).
            self.tab_id = None
            try:
                self._prepare_tab()
                prompt = serialize_messages(messages)
                self._submit(prompt)
                yield from self._watch()
            except BridgeError:
                raise
            except Exception as e:
                raise BridgeError(f"bridge failure: {e}") from e

    # ---- internals -------------------------------------------------

    def _drain_queue(self) -> None:
        """Discard any queued acks from a previous (abandoned) request."""
        while True:
            try:
                self.server.result_queue.get_nowait()
            except queue.Empty:
                break

    def _prepare_tab(self) -> None:
        if not self.new_tab:
            return

        payload: dict = {"url": "https://chat.deepseek.com/"}
        if self.container is not None:
            payload["container"] = self.container
        if self.container_id is not None:
            payload["container_id"] = self.container_id

        container_desc = ""
        if self.container:
            container_desc = f" in container {self.container!r}"
        elif self.container_id:
            container_desc = f" in container-id {self.container_id!r}"
        print(f"[daemon] opening new DeepSeek tab{container_desc}",
              file=sys.stderr)

        result = self._call(
            "open-tab",
            payload,
            ack="open-tab",
            timeout=TAB_OPEN_TIMEOUT,
        )
        # The extension acks with `success: false` when, e.g., the
        # requested container name doesn't resolve or matches more
        # than one container.  Before this check, the daemon
        # proceeded with `tab_id = None` and later fell back to
        # "active tab" semantics in submit/watch — silently using
        # whichever DeepSeek tab the user happened to have focused,
        # possibly outside the requested container.  Raising here
        # surfaces the extension's own reason directly.
        if not result.get("success"):
            raise BridgeError(
                f"open-tab failed: "
                f"{result.get('reason') or '(no reason)'}"
            )
        self.tab_id = result.get("tab_id")
        cs_id = result.get("cookie_store_id")
        detail = f"tab {self.tab_id} -> {result.get('url')}"
        if cs_id:
            detail += f"  (container: {cs_id})"
        print(f"[daemon] opened {detail}", file=sys.stderr)
        if self.tab_wait > 0:
            print(
                f"[daemon] waiting {self.tab_wait:.1f}s for tab load",
                file=sys.stderr,
            )
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
            # Thinking phase can run 60–120 s with no answer content.
            # 30 s was too aggressive; the injected-watch code already
            # treats "Stop button visible" as activity, so this timeout
            # now only fires on genuine idle.  120 s matches realistic
            # worst-case thinking.
            "first_content_timeout_ms": 120000,
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
                # NEW (2026-11-01): periodic heartbeat so the user
                # can tell "browser is generating (thought phase)"
                # from "stuck".  The heartbeat does NOT extend the
                # deadline — WATCH_MAX_WAIT still bounds the total
                # wait — it only makes the wait legible.  Rationale:
                # the first-content timeout in the extension is now
                # an *idle* timeout (see injected-watch.js), so a
                # thinking phase can legitimately run for tens of
                # seconds with no watch-delta arriving.
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
                if total == 0:
                    # Distinguish real causes so the caller (and the
                    # user reading stderr) knows what went wrong.  A
                    # silent zero-char success is the failure shape
                    # that produced the 2026-10-31 "daemon saw nothing"
                    # incident — surfacing the reason turns a 120 s
                    # silence into an actionable message.
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
                            "model produced no output within 120s — the "
                            "prompt may not have been submitted, or the "
                            "web UI is not streaming to IndexedDB. "
                            "Check the tab."
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
