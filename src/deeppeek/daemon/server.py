"""OpenAI-compatible HTTP server — the facade aider dials into."""
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class OpenAIHandler(BaseHTTPRequestHandler):
    bridge = None  # set by ``serve()`` before ``serve_forever`` runs

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
            self._json(400, _error(f"bad json: {e}", "invalid_request_error"))
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
                usage = _usage_estimate(messages, content)
                self._json(200, _completion_payload(model, content, usage))
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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        first = True

        try:
            for delta in self.bridge.stream(messages, model):
                if not delta:
                    continue
                chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": (
                            {"role": "assistant", "content": delta}
                            if first else {"content": delta}
                        ),
                        "finish_reason": None,
                    }],
                }
                first = False
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()

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
            # start of the next request.
            return

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


def serve(bridge, port: int = 8766) -> None:
    """Blocking serve loop.  Caller binds ``bridge``, then handles
    Ctrl+C and shuts down ``ExportServer`` in its own finally block."""
    OpenAIHandler.bridge = bridge
    httpd = ThreadingHTTPServer(("127.0.0.1", port), OpenAIHandler)
    httpd.daemon_threads = True
    httpd.serve_forever()