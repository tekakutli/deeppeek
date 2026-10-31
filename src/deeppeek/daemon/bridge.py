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
WATCH_MAX_WAIT = 190.0


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
    ):
        self.server = server
        self.new_tab = new_tab
        self.tab_wait = tab_wait
        self.poll_interval_ms = poll_interval_ms
        self.settle_ticks = settle_ticks
        self.max_duration_ms = max_duration_ms
        self._lock = threading.Lock()

    # ---- public ----------------------------------------------------

    def complete(self, messages: list[dict], model: str) -> str:
        """Blocking, non-streaming completion."""
        return "".join(self.stream(messages, model))

    def stream(self, messages: list[dict], model: str):
        """Generator of content deltas."""
        with self._lock:
            self._drain_queue()
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
        print("[daemon] opening new DeepSeek tab", file=sys.stderr)
        self._call(
            "open-tab",
            {"url": "https://chat.deepseek.com/"},
            ack="open-tab",
            timeout=TAB_OPEN_TIMEOUT,
        )
        if self.tab_wait > 0:
            print(
                f"[daemon] waiting {self.tab_wait:.1f}s for tab load",
                file=sys.stderr,
            )
            time.sleep(self.tab_wait)

    def _submit(self, prompt: str) -> None:
        print(f"[daemon] submitting {len(prompt):,} chars", file=sys.stderr)
        last_error: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2.0)
                print(
                    f"[daemon] submit retry {attempt}/2", file=sys.stderr,
                )
            try:
                result = self._call(
                    "submit", {"text": prompt},
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
        self.server._broadcast("watch", {
            "batch_id": batch_id,
            "include_forked": False,
            "poll_interval_ms": self.poll_interval_ms,
            "settle_ticks": self.settle_ticks,
            "max_duration_ms": self.max_duration_ms,
        })

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
        deadline = time.monotonic() + WATCH_MAX_WAIT
        while time.monotonic() < deadline:
            try:
                msg = self.server.wait_for_result(timeout=0.5)
            except queue.Empty:
                continue
            if msg.get("batch_id") != batch_id:
                continue
            ack = msg.get("__ack")
            if ack == "watch-delta":
                delta = msg.get("delta", "")
                if delta:
                    total += len(delta)
                    yield delta
            elif ack == "watch-rewrite":
                content = msg.get("content", "")
                print(
                    f"[daemon] watch-rewrite ({len(content)} chars) — "
                    f"passing through",
                    file=sys.stderr,
                )
                total += len(content)
                yield content
            elif ack == "watch-done":
                if total == 0:
                    print(
                        "[daemon] WARNING: model returned empty response",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[daemon] watch done ({total:,} chars)",
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