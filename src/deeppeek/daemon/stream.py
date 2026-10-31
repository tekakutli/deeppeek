"""Fallback delta poller — kept in sync with the extension's `watch`
event so the bridge has a documented alternative if `watch` breaks.

The bridge's PRIMARY streaming strategy is the extension's `watch`
event (defined in ``background.js``), which polls IndexedDB inside
the browser and POSTs only deltas back to ``/export``.  This module
provides the equivalent logic on the Python side, so the bridge can
fall back to an ``extract`` + diff loop if needed.

Currently unused by the bridge; kept because:
  · a practical escape hatch if a Zen/DeepSeek update breaks `watch`;
  · a second, independent implementation of the delta semantics.
"""
import time
from dataclasses import dataclass


@dataclass
class Delta:
    content: str
    done: bool = False
    reason: str | None = None


def poll_until_done(
    extract_fn,
    *,
    poll_interval: float = 0.3,
    settle_time: float = 1.5,
    max_wait: float = 180.0,
    first_content_timeout: float = 60.0,
):
    """Yield Deltas as the last assistant message grows.

    ``extract_fn`` must return the extension's extract payload
    (``{"messages": [{"role": "assistant", "content": "..."}]}``),
    or None/empty when nothing is extractable yet — e.g. a just-opened
    tab has no session ID until the prompt is submitted.

    Completion heuristic: content hasn't changed for ``settle_time``
    seconds AFTER we first saw non-empty content.  Less precise than
    a DOM probe, but stable across UI changes.
    """
    last_sent = ""
    last_change: float | None = None
    start = time.monotonic()

    while True:
        elapsed = time.monotonic() - start
        if elapsed > max_wait:
            yield Delta(content="", done=True, reason="timeout")
            return
        if last_change is None and elapsed > first_content_timeout:
            yield Delta(content="", done=True, reason="no_content")
            return

        payload = extract_fn()
        if payload:
            content = ""
            for m in reversed(payload.get("messages", [])):
                if m.get("role") == "assistant":
                    content = m.get("content") or ""
                    break

            if content:
                if last_change is None:
                    last_change = time.monotonic()
                if content.startswith(last_sent):
                    delta = content[len(last_sent):]
                    if delta:
                        last_sent = content
                        last_change = time.monotonic()
                        yield Delta(content=delta)
                else:
                    # The message was rewritten.  Emit the whole
                    # content as a correction — aider may see the
                    # earlier deltas AND this, but the alternative
                    # (silently dropping content) is worse.
                    last_sent = content
                    last_change = time.monotonic()
                    yield Delta(content=content)

        if (last_change is not None
                and (time.monotonic() - last_change) >= settle_time):
            yield Delta(content="", done=True)
            return

        time.sleep(poll_interval)