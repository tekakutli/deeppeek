#!/usr/bin/env python3
"""Probe the deeppeek daemon and the extension it depends on.

Three independent checks, each optional via flags:

  1. OpenAI façade     GET  http://127.0.0.1:8766/v1/models
  2. Extension status  GET  http://127.0.0.1:8765/status
  3. End-to-end        POST http://127.0.0.1:8766/v1/chat/completions
                       with a trivial prompt, printing the reply

Exit codes:
  0  all requested checks passed
  1  at least one check failed
  2  argument error

Usage:
    daemon_probe.py                  # checks 1 and 2
    daemon_probe.py --e2e            # also check 3 (sends a real prompt)
    daemon_probe.py --port 9000      # custom daemon port
    daemon_probe.py --no-extension   # only probe the OpenAI façade

The script never imports `deeppeek`.  It talks to the daemon over
HTTP exactly the way `aider` would, so a passing run means a passing
aider run.  Uses only the standard library — same constraint as
every other script under `scripts/`.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def _get(url: str, timeout: float = 2.0) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def _post_json(url: str, payload: dict,
               timeout: float = 120.0) -> tuple[int | None, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer dummy",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def check_openai(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/v1/models"
    status, body = _get(url)
    if status != 200:
        print(f"  [FAIL] {url} -> {status} {body[:200]}",
              file=sys.stderr)
        return False
    try:
        data = json.loads(body)
        ids = [m.get("id") for m in data.get("data", [])]
    except json.JSONDecodeError:
        print(f"  [FAIL] {url} returned non-JSON", file=sys.stderr)
        return False
    print(f"  [ OK ] {url} -> models={ids}", file=sys.stderr)
    return True


def check_extension(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/status"
    status, body = _get(url)
    if status != 200:
        print(f"  [FAIL] {url} -> {status} {body[:200]}",
              file=sys.stderr)
        return False
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(f"  [FAIL] {url} returned non-JSON", file=sys.stderr)
        return False
    armed = data.get("armed")
    clients = data.get("sse_clients", 0)
    ok = clients > 0
    mark = " OK " if ok else "FAIL"
    print(f"  [{mark}] {url} -> sse_clients={clients} armed={armed}",
          file=sys.stderr)
    return ok


def check_e2e(port: int, model: str, timeout: float) -> bool:
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "user",
             "content": "Reply with exactly the word: pong"},
        ],
        "stream": False,
    }
    print(f"  sending test prompt to {url} "
          f"(timeout {timeout:.0f}s)...", file=sys.stderr)
    status, body = _post_json(url, payload, timeout)
    if status != 200:
        print(f"  [FAIL] {url} -> {status} {body[:400]}",
              file=sys.stderr)
        return False
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  [FAIL] bad response shape: {e}; body={body[:400]}",
              file=sys.stderr)
        return False

    # Empty content is a distinct failure from a bad response shape.
    # Before this check, a zero-char reply (the 2026-10-31 daemon-saw-
    # nothing incident) printed "[ OK ] reply (0 chars): ''" and
    # passed — misleading, because the upstream daemon had not
    # actually collected any model output.
    if not content.strip():
        print(
            f"  [FAIL] empty reply (daemon returned no content — "
            f"watch timed out or browser didn't stream)",
            file=sys.stderr,
        )
        return False

    preview = content.strip().replace("\n", " ")[:120]
    print(f"  [ OK ] reply ({len(content)} chars): {preview!r}",
          file=sys.stderr)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="daemon_probe.py",
        description="Probe the deeppeek daemon and its extension bridge.",
    )
    ap.add_argument("--port", type=int, default=8766,
                    help="OpenAI façade port (default: 8766)")
    ap.add_argument("--extension-port", type=int, default=8765,
                    help="extension SSE port (default: 8765)")
    ap.add_argument("--model", default="deepseek-web",
                    help="model name for --e2e (default: deepseek-web)")
    ap.add_argument("--e2e", action="store_true",
                    help="also send a real chat completion")
    ap.add_argument("--e2e-timeout", type=float, default=120.0,
                    help="seconds for the end-to-end check (default: 120)")
    ap.add_argument("--no-extension", action="store_true",
                    help="skip the extension /status probe")
    ap.add_argument("--no-openai", action="store_true",
                    help="skip the OpenAI /v1/models probe")
    args = ap.parse_args(argv)

    print("daemon_probe: checking", file=sys.stderr)
    all_ok = True

    if not args.no_openai:
        print("\n[1] OpenAI façade", file=sys.stderr)
        all_ok &= check_openai(args.port)

    if not args.no_extension:
        print("\n[2] Extension transport", file=sys.stderr)
        all_ok &= check_extension(args.extension_port)

    if args.e2e:
        print("\n[3] End-to-end completion", file=sys.stderr)
        all_ok &= check_e2e(args.port, args.model, args.e2e_timeout)

    print(file=sys.stderr)
    if all_ok:
        print("all checks passed", file=sys.stderr)
        return 0
    print("one or more checks failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
