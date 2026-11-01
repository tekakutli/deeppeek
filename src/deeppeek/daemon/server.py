"""OpenAI-compatible HTTP server — the facade aider dials into.

Retry-storm fix (2026-10-31): when the browser bridge fails (submit
blocked, model produced no output, tab gone, etc.), the failure used
to surface as a 500 + `server_error` SSE frame mid-stream.  Aider
retries on that shape — but the underlying cause is *not* transient
(submit blocked by a userscript stays blocked; a wedged model stays
wedged), so retrying only makes things worse: each retry opens a
fresh tab, submits, waits 10-30s, fails again.  Result: a retry
storm.

The fix is twofold:

  1. `_stream_response` now BUFFERS the first content chunk before
     committing SSE headers.  If the bridge raises during that
     buffering window (which covers the whole submit + first-content
     path), we can return a clean HTTP 400 with a JSON error body.

  2. BridgeError → 400 + `invalid_request_error`.  Aider (and most
     OpenAI clients) treat `invalid_request_error` as non-retriable,
     so the storm stops.  5xx / `server_error` (which we still emit
     for genuine bugs, mid-stream failures) remain retriable, since
     those really *can* be transient.

Once the first chunk arrives, headers are sent and the stream runs
normally — mid-stream bridge errors still surface as SSE error
frames (retriable, but at that point partial content has already
been delivered, so retrying is at least defensible).

Output mirroring (`--print-output`, 2026-11-01): when the daemon is
started with `--print-output`, every text delta written to the
client is also echoed to the daemon's own stdout, so the operator
can watch what the model is actually producing without having to
capture the HTTP stream in another process.  Streaming deltas are
printed with `end=""` so stdout reconstructs the reply live; a
newline is appended when the stream completes.  The non-streaming
path prints the full content once.
"""
import itertools
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bridge import BridgeError


class OpenAIHandler(BaseHTTPRequestHandler):
    bridge = None  # set by ``serve()`` before ``serve_forever`` runs
    print_output = False  # set by ``serve()``; mirrors --print-output

    def log_message(self, *args, **kwargs):
        pass  # keep the terminal quiet; aider has its own logs

    # ---- HTTP verbs ------------------------------------------------

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, _error("not found", "invalid_request_error"))
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            self._json(400, _error(f"bad json: {e}",
                                   "invalid_request_error"))
            return

        model = req.get("model", "deepseek-web")
        messages = req.get("messages") or []
        stream = bool(req.get("stream", False))

        if not messages:
            self._json(
                400,
                _error("messages is required", "invalid_request_error"),
            )
            return

        try:
            if stream:
                self._stream_response(model, messages)
            else:
                content = self.bridge.complete(messages, model)
                if self.print_output:
                    print(content, flush=True)
                usage = _usage_estimate(messages, content)
                self._json(200, _completion_payload(model, content, usage))
        except BridgeError as e:
            # Non-retriable — retrying won't help (submit blocked,
            # model wedged, tab gone).  Return 400 with
            # invalid_request_error so aider does not loop.
            self._json(400, _error(str(e), "invalid_request_error"))
        except TimeoutError as e:
            self._json(504, _error(str(e), "timeout"))
        except Exception as e:
            self._json(500, _error(str(e), "server_error"))

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {
                "object": "list",
                "data": [
                    {
                        "id": "deepseek-web",
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "deeppeek",
                    },
                ],
            })
        else:
            self._json(404, _error("not found", "invalid_request_error"))

    # ---- streaming -------------------------------------------------

    def _stream_response(self, model: str, messages: list[dict]):
        # Buffer the first chunk before committing SSE headers so a
        # BridgeError during the submit + first-content window can be
        # reported as a clean HTTP 400 instead of a mid-stream SSE
        # error frame (which aider treats as retriable).
        try:
            bridge_gen = self.bridge.stream(messages, model)
            first = next(bridge_gen, None)
        except BridgeError as e:
            self._json(400, _error(str(e), "invalid_request_error"))
            return
        except Exception as e:
            self._json(500, _error(str(e), "server_error"))
            return

        if first is None or first == "":
            self._json(400, _error(
                "model produced no output (bridge yielded nothing)",
                "invalid_request_error",
            ))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        first_chunk = True

        try:
            for delta in itertools.chain([first], bridge_gen):
                if not delta:
                    continue
                if self.print_output:
                    # Reconstruct the reply on the daemon's own
                    # stdout.  No trailing newline per delta — a
                    # single newline is emitted after the stream
                    # completes (below), so concurrent CLI output
                    # stays readable and a captured stdout file
                    # matches the assistant reply exactly.
                    print(delta, end="", flush=True)
                chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": (
                            {"role": "assistant", "content": delta}
                            if first_chunk else {"content": delta}
                        ),
                        "finish_reason": None,
                    }],
                }
                first_chunk = False
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()

            if self.print_output:
                print(flush=True)

            final = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }],
            }
            self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client (aider) disconnected.  The extension's watch loop
            # times out on its own; queued deltas are drained at the
            # start of the next request.  No trailing newline —
            # the operator asked for the reply, and it was truncated
            # by the client, so a newline here would lie about
            # completeness.
            return
        except Exception as e:
            # Headers already on the wire; can't change status.
            # Send an SSE error frame + [DONE] so the client sees a
            # clean end instead of a half-written response.
            try:
                err = {"error": {"message": str(e),
                                 "type": "server_error"}}
                self.wfile.write(f"data: {json.dumps(err)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _completion_payload(model: str, content: str, usage: dict) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


def _usage_estimate(messages: list[dict], content: str) -> dict:
    """Rough token estimate (4 chars/token heuristic)."""
    prompt_chars = sum(
        len(_as_str(m.get("content"))) for m in messages
    )
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, len(content) // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _as_str(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            (p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _error(message: str, err_type: str) -> dict:
    return {"error": {"message": message, "type": err_type}}


def serve(bridge, port: int = 8766, print_output: bool = False) -> None:
    """Blocking serve loop.  Caller binds ``bridge``, then handles
    Ctrl+C and shuts down ``ExportServer`` in its own finally block.

    ``print_output`` — when True, every text delta the daemon
    streams back to the client (aider) is echoed to the daemon's
    own stdout.  Streaming deltas are printed with ``end=""`` so
    stdout reconstructs the reply live; a newline is appended when
    the stream completes.  The non-streaming path prints the full
    content once.  Useful for debugging what the model is actually
    producing without having to capture the HTTP stream
    separately.
    """
    OpenAIHandler.bridge = bridge
    OpenAIHandler.print_output = print_output
    httpd = ThreadingHTTPServer(("127.0.0.1", port), OpenAIHandler)
    httpd.daemon_threads = True
    httpd.serve_forever()
