import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ExportServer:
    """
    Local HTTP server.  Roles:

      /events   SSE stream — arms/disarms the extension, carries
                one-shot `config` / `request-status` events (watchdog
                subcommand), and `attach` events (pipe subcommand).
      /export   POST endpoint — extension pushes payloads or acks.
      /status   plain JSON for debugging.
    """

    def __init__(self, port: int = 8765, watch_mode: bool = False):
        self.port = port
        self.watch_mode = watch_mode
        self.result_queue: "queue.Queue[dict]" = queue.Queue()
        self.armed = watch_mode
        self.sse_clients: list["queue.Queue[dict]"] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._httpd = None
        self._thread = None

    # ---- SSE plumbing ---------------------------------------------

    def _add_sse_client(self) -> "queue.Queue[dict]":
        q: "queue.Queue[dict]" = queue.Queue(maxsize=100)
        with self._lock:
            self.sse_clients.append(q)
            armed_now = self.armed
        if armed_now:
            q.put_nowait({"event": "arm"})
        return q

    def _remove_sse_client(self, q: "queue.Queue[dict]") -> None:
        with self._lock:
            try:
                self.sse_clients.remove(q)
            except ValueError:
                pass

    def _broadcast(self, event_name: str, data: dict = None) -> None:
        with self._lock:
            clients = list(self.sse_clients)
        msg = {"event": event_name}
        if data is not None:
            msg["data"] = data
        for q in clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def has_extension(self) -> bool:
        with self._lock:
            return bool(self.sse_clients)

    def wait_for_extension(self, timeout: float) -> bool:
        """Block up to `timeout` seconds waiting for any SSE client."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.has_extension():
                return True
            time.sleep(0.05)
        return False

    def broadcast_attach(self, files: list[dict], batch_id: str,
                        delay_ms: int = 0) -> None:
        """Push an attach event to all connected extension clients.

        `files` is a list of {"name", "mime", "base64"} dicts.
        `delay_ms == 0` → batch; `> 0` → serial with the given delay.
        """
        self._broadcast("attach", {
            "batch_id": batch_id,
            "files": files,
            "opts": {"delay_ms": delay_ms},
        })

    # ---- lifecycle ------------------------------------------------

    def start(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass

            def _send_json(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type")
                self.end_headers()

            def do_GET(self):
                if self.path == "/events":
                    self._handle_events()
                elif self.path == "/status":
                    with parent._lock:
                        armed = parent.armed
                        n_clients = len(parent.sse_clients)
                    self._send_json(200, {
                        "ok": True,
                        "armed": armed,
                        "sse_clients": n_clients,
                    })
                else:
                    self._send_json(404, {"error": "not found"})

            def _handle_events(self):
                q = parent._add_sse_client()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Accel-Buffering", "no")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()

                    self.wfile.write(b"retry: 1000\n\n")
                    self.wfile.flush()

                    last_heartbeat = time.monotonic()
                    HEARTBEAT_INTERVAL_S = 15

                    while not parent._stop_event.is_set():
                        try:
                            msg = q.get(timeout=1.0)
                            event_name = msg.get("event", "message")
                            data = msg.get("data")
                            payload = json.dumps(data) if data is not None else "{}"
                            self.wfile.write(
                                f"event: {event_name}\ndata: {payload}\n\n"
                                .encode("utf-8")
                            )
                            self.wfile.flush()
                        except queue.Empty:
                            pass

                        now = time.monotonic()
                        if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                            try:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                                last_heartbeat = now
                            except (BrokenPipeError, ConnectionResetError,
                                    OSError):
                                break
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    parent._remove_sse_client(q)

            def do_POST(self):
                if self.path == "/export":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        data = json.loads(raw)
                    except Exception as e:
                        self._send_json(400, {"error": f"bad json: {e}"})
                        return
                    with parent._lock:
                        was_armed = parent.armed
                        parent.armed = False
                    parent.result_queue.put(data)
                    if was_armed:
                        parent._broadcast("disarm")
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(404, {"error": "not found"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def wait_for_result(self, timeout=None) -> dict:
        return self.result_queue.get(timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
            self._thread = None
