"""
recover.llm — LLM client with health probe, per-call timeout, and
live-call logging.

STABLE.  Only changes when the wire format for the OpenAI-compatible
endpoint changes.
"""

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .core import (
    DEFAULT_LLM_URL, DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT, DEFAULT_PROBE_TIMEOUT,
)


class LLMClient:
    def __init__(self, url: str = DEFAULT_LLM_URL,
                 model: str = DEFAULT_LLM_MODEL,
                 cache_dir: Optional[Path] = None,
                 max_calls: Optional[int] = None,
                 timeout: int = DEFAULT_LLM_TIMEOUT):
        self.url = url
        self.model = model
        self.cache_dir = cache_dir
        self.max_calls = max_calls
        self.timeout = timeout
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
        reachability."""
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
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
                resp.read()
            return True
        except Exception as e:
            print(f"  [LLM] probe failed: {e}", file=sys.stderr)
            return False

    def call(self, prompt: str, max_tokens: int = 512,
             temperature: float = 0.2,
             tag: str = "") -> Optional[str]:
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

        print(f"  [LLM:{tag or '?'}] live call {self.calls_made + 1} "
              f"(prompt {len(prompt)} chars)", file=sys.stderr)

        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
            dt = time.monotonic() - t0
            content = data["choices"][0]["message"]["content"]
            self.calls_made += 1
            print(f"  [LLM:{tag or '?'}] ok ({dt:.1f}s)", file=sys.stderr)
            if cp:
                cp.write_text(json.dumps({"content": content}))
            return content
        except Exception as e:
            print(f"  [LLM:{tag or '?'}] call failed: {e}", file=sys.stderr)
            return None
