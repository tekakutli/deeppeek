import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ExportServer:
    """
    Tiny HTTP server. The extension POSTs the extracted conversation to /export.
    /poll returns {"action": "extract"} only in --watch mode AND only until the
    first /export arrives; after that it returns {"action": "idle"} so the
    extension stops re-triggering.  Without this one-shot latch, --watch mode
    tells the extension to sweep the page every 3 seconds forever, which
    produces a visible bottom→top→bottom bounce on the DeepSeek tab and blocks
    the CLI on wait_for_result() if the first sweep never completes.
    """

    def __init__(self, port: int = 8765, watch_mode: bool = False):
        self.port = port
        self.watch_mode = watch_mode
        self.result_queue: "queue.Queue[dict]" = queue.Queue()
        self._watch_fired = False          # NEW: one-shot latch
        self._httpd = None
        self._thread = None

    def start(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass

            def _send_json(self, code, obj):
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
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_GET(self):
                if self.path == "/poll":
                    # One-shot: only the first poll while unwatched gets
                    # "extract".  After /export has been received, all
                    # polls return idle, so the extension stops sweeping.
                    if parent.watch_mode and not parent._watch_fired:
                        action = "extract"
                    else:
                        action = "idle"
                    self._send_json(200, {"action": action})
                elif self.path == "/status":
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self):
                if self.path == "/export":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        data = json.loads(raw)
                    except Exception as e:
                        self._send_json(400, {"error": f"bad json: {e}"})
                        return
                    parent._watch_fired = True   # NEW: latch on
                    parent.result_queue.put(data)
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(404, {"error": "not found"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def wait_for_result(self, timeout=None) -> dict:
        return self.result_queue.get(timeout=timeout)

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
            self._thread = None
