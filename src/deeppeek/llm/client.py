"""
llm.client — LLM client with health probe, per-call timeout, and
live-call logging.

STABLE.  Only changes when the wire format for the OpenAI-compatible
endpoint changes.

The default constants live here so a caller that omits `url` /
`model` / `timeout` still gets something sensible.  Change them in
this file if your local server's defaults differ.

Tab hints (2026-11-23)
----------------------
`call()` accepts an optional `deeppeek_hints` dict that is merged
into the request body under the key `"deeppeek"`.  Non-deeppeek
servers (llama.cpp, llama-swap pass-through, OpenAI) ignore unknown
body fields, so this is safe against any backend.  The deeppeek
daemon reads `deeppeek.tab` to override its per-request tab
behaviour (container, reuse, tab_id).

Probe short-circuit (2026-11-23)
--------------------------------
The deeppeek daemon's `/v1/chat/completions` triggers a full
browser cycle (open tab, submit, stream).  That makes the probe —
whose job is only to confirm the endpoint is up — expensive and
observable (opens a tab!).  The client now sends an
`X-Deeppeek-Probe: 1` header on the probe POST; a deeppeek daemon
sees it and returns a stub completion without touching the
browser.  Standard OpenAI-compatible backends (llama-swap,
llama.cpp, vLLM, ollama) ignore unknown headers, so the probe
behaves exactly as before against them — cold-start warmup intact.

If llama-swap (or another proxy) is in front of the deeppeek
daemon, the header passes through unchanged and the daemon still
short-circuits.
"""

import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Defaults (relocated from recover/core.py — adjust as needed)
# ---------------------------------------------------------------------------

# llama-swap endpoint (see ~/files/org/dotfiles/llama-swap/config.yaml).
# llama-swap lazy-loads models on first request, so the first call after
# startup can block for 30–90s while the gguf mmaps and CUDA context warms.
DEFAULT_LLM_URL = "http://localhost:6006/v1/chat/completions"

# Model key from llama-swap config.yaml — must match a `models:` entry
# exactly, not the gguf filename.
DEFAULT_LLM_MODEL = "qwen3.5-35b-heretic-mtp-mini"

# Per-call timeout in seconds.  None = no timeout — wait however long the
# model needs for cold-start + generation.
DEFAULT_LLM_TIMEOUT = None

# Health-probe timeout.  Deliberately generous so a cold-start warmup on
# the first probe doesn't false-negative.  A truly-down endpoint still
# fails fast because the TCP connect is refused immediately.
DEFAULT_PROBE_TIMEOUT = 180


def _http_error_body(e: urllib.error.HTTPError) -> str:
    """Return the response body of an HTTPError as text, or '' on failure."""
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return ""


class LLMClient:
    def __init__(self, url: str = DEFAULT_LLM_URL,
                 model: str = DEFAULT_LLM_MODEL,
                 cache_dir: Optional[Path] = None,
                 max_calls: Optional[int] = None,
                 timeout: Optional[int] = DEFAULT_LLM_TIMEOUT,
                 stream: bool = True):
        self.url = url
        self.model = model
        self.cache_dir = cache_dir
        self.max_calls = max_calls
        self.timeout = timeout
        self.stream_default = stream
        self.calls_made = 0
        self.calls_cached = 0
        self.calls_refused = 0
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, prompt: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        h = hashlib.sha256(
            (self.model + "\n" + prompt).encode()
        ).hexdigest()[:16]
        return self.cache_dir / f"{h}.json"

    def probe(self, probe_timeout: int = DEFAULT_PROBE_TIMEOUT) -> bool:
        """Quick health check; bypasses cache so it reflects current
        reachability.

        Sends `X-Deeppeek-Probe: 1`.  A deeppeek daemon sees the
        header and returns a stub completion without opening a
        browser tab; other backends ignore the header and this
        behaves as the historical cold-start warmup POST.
        """
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Deeppeek-Probe": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as e:
            detail = _http_error_body(e)
            print(
                f"  [LLM] probe failed: HTTP {e.code} {e.reason}"
                + (f"\n  {detail}" if detail else ""),
                file=sys.stderr)
            return False
        except Exception as e:
            print(f"  [LLM] probe failed: {e}", file=sys.stderr)
            return False

    def _stream_call(self, req, tag: str):
        """Execute a streaming (SSE) POST; print deltas to stderr live.

        Returns (content, dt) on success, or (None, dt) on failure.
        A failure is either an HTTP-level error already raised by
        urlopen, or an SSE `data: {"error": ...}` frame received from
        the server with no accompanying content chunks.  An *empty*
        successful reply (chunks received but all empty) returns
        ("", dt) — that's a legitimate (if unhelpful) result, not an
        error.

        Wire-format notes:

          · Lines starting with `:` are SSE comments / keepalives and
            are skipped silently (spec: RFC-style comment lines).
          · `data:` with no trailing space and with a single space
            are both accepted.
          · `data: [DONE]` terminates the stream.
          · `data: {"error": {...}}` marks a server-side failure;
            the client keeps reading until [DONE] to drain the
            connection, then reports failure if no content was
            received.
        """
        t0 = time.monotonic()
        chunks: list[str] = []
        started = False
        first_chunk_at: Optional[float] = None
        total_lines = 0
        data_lines = 0
        keepalive_lines = 0
        saw_error: Optional[dict] = None

        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            while True:
                raw = resp.readline()
                if not raw:
                    break
                total_lines += 1
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                if line.startswith(":"):
                    keepalive_lines += 1
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                data_lines += 1
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError as e:
                    print(
                        f"  [LLM:{tag or '?'}] WARN: bad SSE payload "
                        f"({e}): {payload[:120]!r}",
                        file=sys.stderr,
                    )
                    continue
                # Server-side error frame — remember it, keep draining
                # so the connection closes cleanly.
                err = obj.get("error")
                if err:
                    saw_error = err if isinstance(err, dict) else {
                        "message": str(err)}
                    print(
                        f"  [LLM:{tag or '?'}] SSE error: "
                        f"{saw_error.get('type', '?')}: "
                        f"{saw_error.get('message', saw_error)}",
                        file=sys.stderr,
                    )
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece is None:
                    msg = choices[0].get("message") or {}
                    piece = msg.get("content")
                if not piece:
                    continue
                if not started:
                    first_chunk_at = time.monotonic()
                    elapsed = first_chunk_at - t0
                    sys.stderr.write(
                        f"\n  [LLM:{tag or '?'}] first chunk after "
                        f"{elapsed:.1f}s > "
                    )
                    started = True
                sys.stderr.write(piece)
                sys.stderr.flush()
                chunks.append(piece)

        if started:
            sys.stderr.write("\n")
            sys.stderr.flush()

        dt = time.monotonic() - t0
        content = "".join(chunks)

        if not content and total_lines > 0 and data_lines == 0:
            print(
                f"  [LLM:{tag or '?'}] WARN: received {total_lines} "
                f"lines, {keepalive_lines} keepalives, 0 data: frames "
                f"— SSE format mismatch? try --no-stream",
                file=sys.stderr,
            )
        if saw_error and not content:
            return None, dt
        return content, dt

    def call(self, prompt: str, max_tokens: int = 512,
             temperature: float = 0.2,
             tag: str = "",
             stream: Optional[bool] = None,
             deeppeek_hints: Optional[dict] = None) -> Optional[str]:
        if stream is None:
            stream = self.stream_default

        cp = self._cache_path(prompt)
        if cp and cp.exists():
            try:
                data = json.loads(cp.read_text())
                self.calls_cached += 1
                return data.get("content")
            except Exception:
                pass

        if self.max_calls is not None and self.calls_made >= self.max_calls:
            self.calls_refused += 1
            print(f"  [LLM:{tag or '?'}] refused: max_calls={self.max_calls} "
                  f"reached", file=sys.stderr)
            return None

        stream_note = ", streaming" if stream else ""
        print(f"  [LLM:{tag or '?'}] live call {self.calls_made + 1} "
              f"(prompt {len(prompt)} chars{stream_note})",
              file=sys.stderr)

        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if deeppeek_hints:
            body["deeppeek"] = deeppeek_hints
        if stream:
            body["stream"] = True
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            t0 = time.monotonic()
            if stream:
                content, dt = self._stream_call(req, tag)
                if content is None:
                    return None
            else:
                with urllib.request.urlopen(
                        req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                dt = time.monotonic() - t0
                content = data["choices"][0]["message"]["content"]
            self.calls_made += 1
            print(f"  [LLM:{tag or '?'}] ok ({dt:.1f}s, "
                  f"{len(content)} chars)", file=sys.stderr)
            if cp:
                cp.write_text(json.dumps({"content": content}))
            return content
        except urllib.error.HTTPError as e:
            detail = _http_error_body(e)
            print(
                f"  [LLM:{tag or '?'}] call failed: HTTP {e.code} {e.reason}"
                + (f"\n  {detail}" if detail else ""),
                file=sys.stderr)
            return None
        except Exception as e:
            print(f"  [LLM:{tag or '?'}] call failed: {e}", file=sys.stderr)
            return None