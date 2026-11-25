"""OpenAI-compatible HTTP server — the facade aider dials into.

120s first-chunk timeout fix (2026-11-01): the previous design
buffered the first content chunk from the bridge *before* committing
SSE headers, so BridgeError during the submit + first-content window
could surface as a clean HTTP 400 (non-retriable, so aider wouldn't
storm).  The tradeoff: while the browser thinks — often >120s — the
client sees zero bytes on the wire, and its own first-chunk timeout
(litellm's 120s default) fires before the daemon has anything to
send.

The fix is to commit SSE headers immediately, emit an empty
role=assistant opening chunk, and then emit SSE keepalive comments
(`: keepalive`) every 15s while the bridge is blocked waiting for
the first real token.  The keepalives are valid SSE per spec
("comment" lines starting with `:`) and are silently ignored by
every spec-compliant parser, so the client sees a live stream
instead of a dead one.  litellm's first-chunk timer now sees data
within 15s and does not fire.

BridgeError is still non-retriable, just via a different mechanism:
since headers are on the wire, we emit an SSE error frame with
`type: "invalid_request_error"`, which litellm maps to
BadRequestError and aider therefore does not retry.  Genuine bugs
(mid-stream exceptions, sse_error) still surface as `server_error`
and remain retriable.

Empty responses (bridge yields nothing before completing) also
still fail-loud: instead of `finish_reason: stop` with empty
content, we send an `invalid_request_error` SSE frame, matching the
old pre-header-buffering behaviour.

Output mirroring (`--print-output`, 2026-11-01): when the daemon is
started with `--print-output`, every text delta written to the
client is also echoed to the daemon's own stdout, so the operator
can watch what the model is actually producing without having to
capture the HTTP stream in another process.  Streaming deltas are
printed with `end=""` so stdout reconstructs the reply live; a
newline is appended when the stream completes.  The non-streaming
path prints the full content once.

Tab hints (2026-11-23)
----------------------
The request body may carry an optional `deeppeek` object:

    {"deeppeek": {"tab": {"mode": ..., "container": ..., ...}}}

`deeppeek.tab` is forwarded verbatim to `BrowserBridge.stream` /
`.complete` as `tab_options`.  Unknown fields are ignored, so an
older bridge stays compatible; unknown `mode` values raise
BridgeError → HTTP 400, matching the invalid-request shape clients
already retry-avoid.

Probe short-circuit (2026-11-23)
--------------------------------
A caller that only wants to confirm reachability — the LLM client's
`probe()`, monitoring, `curl -I` equivalents — sends the header
`X-Deeppeek-Probe: 1`.  The daemon responds with an empty
`chat.completion` payload without invoking the bridge, so no
browser tab is opened and no model tokens are consumed.  The
response shape matches a valid non-streaming completion so standard
clients parse it without special-casing.

Active-tab query (2026-11-24)
-----------------------------
`GET /deeppeek/active-tab` is a deeppeek-specific read-only query
(not part of the OpenAI surface) that asks the extension which
supported chat tab is currently focused.  Backs
`deeppeek daemon --which-tab`, whose output feeds
`--reuse-tab-by TAB_ID`.  Returns the extension's reply verbatim:
`{"success": bool, "tab_id": int|None, "url": str, "reason": str,
"candidates": [...]}`.  Never opens, submits to, or mutates any tab.
"""
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bridge import BridgeError


# How often to emit a keepalive comment while the bridge is blocked.
# Comfortably under every common first-chunk timeout (litellm default
# 120s, httpx default 60s, aider default 120s) — the client sees data
# every 15s and never concludes the socket is dead.
KEEPALIVE_INTERVAL_S = 15.0


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

        # Probe short-circuit.  A caller that only wants to check
        # reachability sets X-Deeppeek-Probe: 1; we return a valid
        # but empty completion without touching the bridge.  The
        # body is read above, so the TCP connection drains cleanly.
        # Unknown headers from non-deeppeek backends are ignored on
        # their side, so nothing else in the ecosystem needs to
        # change.
        if self.headers.get("X-Deeppeek-Probe"):
            model = req.get("model", "deepseek-web")
            empty = _completion_payload(model, "", {
                "prompt_tokens": 1,
                "completion_tokens": 0,
                "total_tokens": 1,
            })
            self._json(200, empty)
            return

        model = req.get("model", "deepseek-web")
        messages = req.get("messages") or []
        stream = bool(req.get("stream", False))

        # Optional per-request tab override.  Non-deeppeek clients
        # (aider, raw curl) simply omit this and the daemon uses its
        # CLI-configured defaults.
        deeppeek = req.get("deeppeek") or {}
        tab_options = deeppeek.get("tab") or None

        if not messages:
            self._json(
                400,
                _error("messages is required", "invalid_request_error"),
            )
            return

        try:
            if stream:
                self._stream_response(model, messages,
                                      tab_options=tab_options)
            else:
                content = self.bridge.complete(messages, model,
                                               tab_options=tab_options)
                if self.print_output:
                    print(content, flush=True)
                usage = _usage_estimate(messages, content)
                self._json(200, _completion_payload(model, content, usage))
        except BridgeError as e:
            # Non-streaming path: this is still a clean 400. Retrying
            # won't help (submit blocked, model wedged, tab gone), so
            # aider must not storm. Streaming path handles this via
            # _sse_error with type=invalid_request_error instead.
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
        elif self.path == "/deeppeek/active-tab":
            # Read-only query backing `deeppeek daemon --which-tab`.
            # Returns the extension's reply verbatim so the client
            # can render success and failure (with `candidates`) the
            # same way.
            try:
                result = self.bridge.get_active_tab()
            except BridgeError as e:
                self._json(400, _error(str(e), "invalid_request_error"))
                return
            except Exception as e:
                self._json(500, _error(str(e), "server_error"))
                return
            self._json(200, result)
        else:
            self._json(404, _error("not found", "invalid_request_error"))

    # ---- streaming -------------------------------------------------

    def _stream_response(self, model: str, messages: list[dict],
                        tab_options: dict | None = None):
        """Stream a bridge response as SSE chunks, emitting keepalives
        while the browser is thinking.

        See the module docstring for the why.  Key mechanics:

          · Headers + empty role chunk are sent immediately, so the
            client never sees a silent socket.
          · The bridge generator runs in a background thread; the main
            thread owns the socket and can emit keepalives.
          · BridgeError surfaces as an SSE error frame with
            type=invalid_request_error (non-retriable, so aider
            doesn't storm).
          · Empty stream (bridge yielded nothing before completing)
            surfaces as an invalid_request_error too, matching the
            old pre-header-buffering behaviour.
        """
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        # Commit SSE headers up front. The old design deferred this
        # until the first content chunk arrived, which is what caused
        # the silent socket that tripped litellm's 120s first-chunk
        # timeout.  We still preserve non-retriability of BridgeError
        # (see _sse_error below).
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Opening chunk: role=assistant, empty content.  Required by
        # the OpenAI streaming protocol so the client sees a valid
        # response open *before* the first content token arrives.
        opening = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }],
        }
        try:
            self.wfile.write(f"data: {json.dumps(opening)}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        # Pump the bridge generator in a background thread so this
        # thread can emit keepalives while the browser is thinking.
        q: "queue.Queue" = queue.Queue()
        pump_error: list[BaseException] = []

        def _pump():
            try:
                for tok in self.bridge.stream(messages, model,
                                              tab_options=tab_options):
                    q.put(tok)
            except BaseException as e:  # noqa: BLE001 (propagated via q)
                pump_error.append(e)
            finally:
                q.put(None)

        threading.Thread(target=_pump, daemon=True).start()

        sent_content = False

        try:
            while True:
                try:
                    tok = q.get(timeout=KEEPALIVE_INTERVAL_S)
                except queue.Empty:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    continue

                if tok is None:
                    if pump_error:
                        e = pump_error[0]
                        err_type = (
                            "invalid_request_error"
                            if isinstance(e, BridgeError)
                            else "server_error"
                        )
                        self._sse_error(str(e), err_type)
                        return
                    if not sent_content:
                        self._sse_error(
                            "model produced no output "
                            "(bridge yielded nothing)",
                            "invalid_request_error",
                        )
                        return
                    break

                if not tok:
                    continue

                if self.print_output:
                    print(tok, end="", flush=True)

                chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": tok},
                        "finish_reason": None,
                    }],
                }
                try:
                    self.wfile.write(
                        f"data: {json.dumps(chunk)}\n\n".encode()
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                sent_content = True

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
            return
        except Exception as e:
            self._sse_error(str(e), "server_error")

    def _sse_error(self, message: str, err_type: str):
        """Emit an SSE error frame + [DONE].  Best-effort — the
        connection may already be gone."""
        try:
            err = {"error": {"message": message, "type": err_type}}
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
    Ctrl+C and shuts down ``ExportServer`` in its own finally block."""
    OpenAIHandler.bridge = bridge
    OpenAIHandler.print_output = print_output
    httpd = ThreadingHTTPServer(("127.0.0.1", port), OpenAIHandler)
    httpd.daemon_threads = True
    httpd.serve_forever()