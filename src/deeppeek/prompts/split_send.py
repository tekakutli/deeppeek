"""`deeppeek prompt split_send` — split a dump and send parts sequentially.

PURPOSE
=======
Load a large markdown dump into DeepSeek's conversation context
across multiple turns.  Each part is attached as a file chip,
followed by a short acknowledgment prompt.  The model's reply is
expected to be brief so the assistant side doesn't burn context.

PIPELINE
========

    bind-tab          pin the pipeline to one specific chat tab
    bind-composer     pin the pipeline to one specific composer element
    split             `dumps_split.py <file> -o <ws>`
                      (fit-parts mode: iteratively increases part count
                      until every part is < DEFAULT_MAX_PART_BYTES,
                      defined in dumps_split.py's CONFIGURATION section)
    for each part:
      clear           idempotent reset before each part
      wait_ready      poll probe-composer until composer ready
      attach          with `wait_upload: true`
      type            in parallel with attach
      enter           adaptive retry
      wait-reply      fast "reply appeared" signal
      (skip ready)    if reply.is_generating == False, skip the probe
      checkpoint      progress.json updated

SPLIT MODE (2026-11-15, sixth iteration)
========================================
Previously, `split_send.py` forced `-n 9` unless the user passed
`-n` or `--target-lines`.  Now it defers to `dumps_split.py`'s
fit-parts default: the split tool computes how many parts are
needed so every chunk is under `DEFAULT_MAX_PART_BYTES` (125 KiB),
checks the largest chunk, and increments parts count until the
whole set fits.

To override:
  · `-n N`             — explicit part count (old behavior)
  · `--target-lines L` — target lines per part
  · `--max-part-bytes N`— fit mode with a custom byte limit
                          (single source of truth: edit
                          DEFAULT_MAX_PART_BYTES in dumps_split.py)

USAGE
=====

    deeppeek prompt split_send /path/to/dump.md
       → splits to fit under 125 KiB per part (default)

    deeppeek prompt split_send dump.md -n 9
       → explicit 9 parts

    deeppeek prompt split_send dump.md --max-part-bytes 200000
       → fit to 200 KB

    deeppeek prompt split_send dump.md --target-lines 3000

    deeppeek prompt split_send dump.md --dry-run
    deeppeek prompt split_send dump.md --tab-id 42
    deeppeek prompt split_send dump.md --resume

Exit codes:
    0  all parts sent successfully
    1  infrastructure failure (connection, bind, split)
    2  at least one part failed (after retries)
    3  argument error
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import queue
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from ..commands._composer import (
    broadcast_and_wait,
    broadcast_many,
    composer_session,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SPLIT_SCRIPT = _PROJECT_ROOT / "scripts" / "dumps" / "dumps_split.py"

DEFAULT_PROMPT_TEMPLATE = "this is part {i}/{n}, just say roger"
PROGRESS_FILE_NAME = "progress.json"

# Legacy fallback — used only by `--no-fit` in dumps_split.py, kept
# here so `--no-fit` behaves identically to the pre-6th-iteration
# default.
DEFAULT_PARTS = 9

_RETRY_DELAYS = [10.0, 30.0, 60.0, 120.0]


def _retry_delay(attempt_number: int) -> float:
    if attempt_number <= 1:
        return 0.0
    idx = min(attempt_number - 2, len(_RETRY_DELAYS) - 1)
    return _RETRY_DELAYS[idx]


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str], *,
             timeout: Optional[float] = None,
             allow_failure: bool = False) -> tuple[int, str, str]:
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        msg = f"`{cmd[0]}` not on PATH"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    except subprocess.TimeoutExpired:
        msg = f"timed out after {timeout}s"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    if proc.returncode != 0:
        tag = "(allowed)" if allow_failure else ""
        print(f"    exit {proc.returncode} {tag}".rstrip(),
              file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:8]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _part_number(path: Path) -> Optional[int]:
    stem = path.stem
    if "_part" not in stem:
        return None
    tail = stem.rsplit("_part", 1)[1]
    try:
        i_str, _n_str = tail.split("of", 1)
        return int(i_str)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Checkpoint file
# ---------------------------------------------------------------------------

def _load_progress(ws: Path, input_sha: str) -> Optional[dict]:
    p = ws / PROGRESS_FILE_NAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("input_sha256") != input_sha:
        print(
            f"[resume] {PROGRESS_FILE_NAME} exists but its input_sha256 "
            f"doesn't match this file; starting fresh",
            file=sys.stderr,
        )
        return None
    return data


def _save_progress(ws: Path, data: dict) -> None:
    p = ws / PROGRESS_FILE_NAME
    try:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[checkpoint] write failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Startup stages
# ---------------------------------------------------------------------------

def _stage_bind_tab(
    server,
    timeout: float,
    explicit_tab_id: Optional[int],
) -> tuple[int, Optional[int]]:
    payload = {}
    if explicit_tab_id is not None:
        payload["tab_id"] = explicit_tab_id

    rc, result = broadcast_and_wait(
        server, "bind-tab", payload,
        timeout=timeout, ack_name="bind-tab",
    )
    if rc != 0:
        reason = (result.get("reason")
                  or result.get("error")
                  or "(no reason)")
        print(f"error: bind-tab failed: {reason}", file=sys.stderr)
        candidates = result.get("candidates")
        if candidates:
            print("  candidates:", file=sys.stderr)
            for c in candidates:
                active = " (active)" if c.get("active") else ""
                print(f"    tab {c['tab_id']}{active}: "
                      f"{c.get('title')!r}  {c.get('url')}",
                      file=sys.stderr)
            print("  pass --tab-id <N> to pick one",
                  file=sys.stderr)
        return 1, None

    tab_id = result.get("tab_id")
    pinned_by = result.get("pinned_by") or "?"
    url = result.get("url") or "?"
    title = result.get("title") or "(untitled)"
    print(f"[bind-tab] pinned tab {tab_id} [{pinned_by}]: "
          f"{title!r}  {url}", file=sys.stderr)
    return 0, tab_id


def _stage_bind_composer(server, tab_id: int, timeout: float) -> int:
    rc, result = broadcast_and_wait(
        server, "bind-composer", {"tab_id": tab_id},
        timeout=timeout, ack_name="bind-composer",
    )
    if rc != 0:
        reason = (result.get("reason")
                  or result.get("error")
                  or "(no reason)")
        print(f"error: bind-composer failed: {reason}", file=sys.stderr)
        tried = result.get("tried_selectors")
        if tried:
            print("  tried:", file=sys.stderr)
            for sel in tried:
                print(f"    {sel}", file=sys.stderr)
        return 1

    selector = result.get("selector") or "?"
    source = result.get("source") or "?"
    print(f"[bind-composer] strict match: {selector} "
          f"(source={source})", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Split stage
# ---------------------------------------------------------------------------

def _stage_split(
    dump_path: Path,
    ws: Path,
    n_parts: Optional[int],
    target_lines: Optional[int],
    max_part_bytes: Optional[int],
    timeout: float,
) -> list[Path]:
    """Invoke `dumps_split.py` and return the part paths.

    Mode resolution (deferred to dumps_split.py):
      · If `n_parts` is not None → pass `-n N` (explicit count).
      · Else if `target_lines` is not None → pass `--target-lines L`.
      · Else if `max_part_bytes` is not None → pass `--max-part-bytes N`.
      · Else → pass nothing; dumps_split.py uses fit mode with its
        default DEFAULT_MAX_PART_BYTES.
    """
    if not _SPLIT_SCRIPT.is_file():
        raise SystemExit(
            f"error: dumps_split.py not found at {_SPLIT_SCRIPT}\n"
            f"  this prompt needs a source checkout; reinstall with "
            f"`pipx install --editable /path/to/deeppeek --force`"
        )
    if not dump_path.is_file():
        raise SystemExit(f"error: not a file: {dump_path}")

    cmd = [sys.executable, str(_SPLIT_SCRIPT), str(dump_path)]
    if n_parts is not None:
        cmd.extend(["-n", str(n_parts)])
    elif target_lines is not None:
        cmd.extend(["--target-lines", str(target_lines)])
    elif max_part_bytes is not None:
        cmd.extend(["--max-part-bytes", str(max_part_bytes)])
    # else: no mode flag → dumps_split.py uses fit mode by default
    cmd.extend(["-o", str(ws)])

    print(f"[split] dumps_split {dump_path.name}", file=sys.stderr)
    rc, _, _ = _run_cmd(cmd, timeout=timeout, allow_failure=False)
    if rc != 0:
        raise SystemExit(f"error: dumps_split exited {rc}")

    numbered: list[tuple[int, Path]] = []
    for p in ws.iterdir():
        if not p.is_file():
            continue
        n = _part_number(p)
        if n is None:
            continue
        numbered.append((n, p))
    if not numbered:
        raise SystemExit(
            f"error: no parts found in {ws} after dumps_split")
    numbered.sort()
    return [p for _, p in numbered]


# ---------------------------------------------------------------------------
# Composer readiness
# ---------------------------------------------------------------------------

def _probe_composer(server, tab_id: int,
                    timeout: float) -> Optional[dict]:
    rc, result = broadcast_and_wait(
        server, "probe-composer", {"tab_id": tab_id},
        timeout=timeout, ack_name="probe-composer",
    )
    if rc != 0:
        return None
    return result


def _wait_for_composer_ready(
    server, tab_id: int, *,
    ready_timeout: float,
    poll_interval: float,
) -> bool:
    deadline = time.monotonic() + ready_timeout
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        probe = _probe_composer(server, tab_id, timeout=5.0)
        if probe is None:
            print(f"  [ready] probe failed (attempt {attempts})",
                  file=sys.stderr)
        else:
            ta = probe.get("textarea_present")
            dis = probe.get("textarea_disabled")
            chips = probe.get("chip_count", -1)
            gen = probe.get("is_generating")
            ready = bool(ta) and not dis and not gen
            if ready:
                return True
            if attempts == 1 or attempts % 4 == 0:
                print(
                    f"  [ready] not yet: "
                    f"textarea_present={ta} disabled={dis} "
                    f"chips={chips} generating={gen}",
                    file=sys.stderr,
                )
        time.sleep(poll_interval)
    return False


# ---------------------------------------------------------------------------
# Reply-detection (fast path)
# ---------------------------------------------------------------------------

def _wait_for_reply(
    server,
    tab_id: int,
    baseline_msg_count: int,
    *,
    reply_timeout: float,
    min_reply_chars: int,
) -> tuple[int, dict]:
    return broadcast_and_wait(
        server, "wait-for-reply",
        {
            "tab_id": tab_id,
            "opts": {
                "baseline_msg_count": baseline_msg_count,
                "min_reply_chars": min_reply_chars,
                "max_duration_ms": int(reply_timeout * 1000),
                "poll_interval_ms": 200,
            },
        },
        timeout=reply_timeout + 5,
        ack_name="wait-for-reply",
    )


# ---------------------------------------------------------------------------
# Per-part send
# ---------------------------------------------------------------------------

def _send_one_part(
    server,
    part_path: Path,
    idx: int,
    total: int,
    text: str,
    tab_id: int,
    *,
    attach_timeout: float,
    watch_timeout: float,
    reply_timeout: float,
    min_reply_chars: int,
    no_wait: bool,
    use_watch: bool,
    upload_timeout: float,
    upload_min_wait: float,
    enter_attempts: int,
    outstanding_watches: list[str],
) -> dict:
    print(
        f"\n[send] part {idx}/{total}: {part_path.name} "
        f"({part_path.stat().st_size:,} bytes)",
        file=sys.stderr,
    )

    # ---- clear ----
    rc, result = broadcast_and_wait(
        server, "clear",
        {"tab_id": tab_id, "opts": {"strict_composer": True}},
        timeout=attach_timeout, ack_name="clear",
    )
    if rc != 0:
        return {
            "success": False, "stage": "clear",
            "reason": result.get("reason") or result.get("error"),
            "details": result,
        }
    print(f"  [ok]   clear", file=sys.stderr)

    # ---- attach (wait_upload) + type in parallel ----
    data = part_path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    specs = [
        {
            "tag": "attach",
            "event": "attach",
            "payload": {
                "tab_id": tab_id,
                "files": [{
                    "name": part_path.name,
                    "mime": "text/markdown",
                    "base64": b64,
                }],
                "opts": {
                    "delay_ms": 0,
                    "strict_composer": True,
                    "wait_upload": True,
                    "wait_upload_timeout_ms": int(upload_timeout * 1000),
                    "wait_upload_min_ms": int(upload_min_wait * 1000),
                },
            },
            "ack_name": "attach",
        },
        {
            "tag": "type",
            "event": "type",
            "payload": {
                "tab_id": tab_id,
                "text": text,
                "opts": {
                    "strict_composer": True,
                    "skip_verify": False,
                    "settle_ms": 300,
                },
            },
            "ack_name": "type",
        },
    ]
    results = broadcast_many(server, specs, timeout=attach_timeout)
    attach_result: dict = {}
    for tag in ("attach", "type"):
        r = results.get(tag, {"exit_code": 1, "result": {}})
        if r["exit_code"] != 0:
            return {
                "success": False, "stage": tag,
                "reason": (r["result"].get("reason")
                           or r["result"].get("error")),
                "details": r["result"],
            }
        if tag == "attach":
            attach_result = r["result"]
            uw = attach_result.get("upload_wait")
            if uw:
                print(
                    f"  [ok]   attach (upload "
                    f"{uw.get('waited_ms', '?')}ms, "
                    f"method={uw.get('method', '?')})",
                    file=sys.stderr,
                )
            else:
                print(f"  [ok]   attach", file=sys.stderr)
        else:
            print(f"  [ok]   {tag}", file=sys.stderr)

    # ---- enter ----
    rc, result = broadcast_and_wait(
        server, "enter",
        {
            "tab_id": tab_id,
            "opts": {
                "strict_composer": True,
                "expected_text": text,
                "enter_attempts": enter_attempts,
            },
        },
        timeout=attach_timeout, ack_name="enter",
    )
    if rc != 0:
        return {
            "success": False, "stage": "enter",
            "reason": (result.get("reason")
                       or result.get("error")
                       or "enter failed (no reason given)"),
            "details": {
                **result,
                "attach_upload_wait": attach_result.get("upload_wait"),
            },
        }
    entered_via = result.get("entered_via") or "?"
    attempts_used = result.get("attempts_used", 1)
    focus_ok = result.get("focus_ok")
    baseline_msg_count = result.get("msg_count_before", 0)
    focus_str = "focus=ok" if focus_ok else "focus=FAILED"
    print(f"  [ok]   enter ({entered_via}, {focus_str}, "
          f"attempts={attempts_used}, baseline={baseline_msg_count})",
          file=sys.stderr)

    if no_wait:
        print(f"  [skip] wait (--no-wait)", file=sys.stderr)
        return {
            "success": True, "stage": "complete",
            "reason": None, "details": {"no_wait": True},
        }

    # ---- wait for reply ----
    if not use_watch:
        print(f"  [reply] waiting for assistant reply "
              f"(baseline={baseline_msg_count})...", file=sys.stderr)
        rc, reply_result = _wait_for_reply(
            server, tab_id, baseline_msg_count,
            reply_timeout=reply_timeout,
            min_reply_chars=min_reply_chars,
        )
        if rc != 0:
            return {
                "success": False, "stage": "wait-reply",
                "reason": (reply_result.get("reason")
                           or reply_result.get("error")
                           or "wait-for-reply failed"),
                "details": {
                    **reply_result,
                    "attach_upload_wait":
                        attach_result.get("upload_wait"),
                },
            }
        waited_ms = reply_result.get("waited_ms", 0)
        reply_len = reply_result.get("reply_length", 0)
        reply_text = reply_result.get("reply_text") or ""
        preview = reply_text[:60].replace("\n", " ")
        gen = reply_result.get("is_generating")
        print(
            f"  [ok]   reply ({waited_ms}ms, {reply_len} chars, "
            f"generating={gen}): {preview!r}",
            file=sys.stderr,
        )
        return {
            "success": True, "stage": "complete",
            "reason": None,
            "details": {
                "reply_wait": reply_result,
                "attach_upload_wait": attach_result.get("upload_wait"),
            },
        }

    # ---- slow path: full watch ----
    print(f"  [watch] waiting for model to finish (--use-watch)...",
          file=sys.stderr)
    batch_id = uuid.uuid4().hex[:12]
    outstanding_watches.append(batch_id)
    server._broadcast("watch", {
        "batch_id": batch_id,
        "tab_id": tab_id,
        "strict_pin": True,
        "include_forked": False,
        "poll_interval_ms": 300,
        "settle_ticks": 5,
        "max_duration_ms": int(watch_timeout * 1000),
        "first_content_timeout_ms": int(watch_timeout * 1000),
    })

    deadline = time.monotonic() + watch_timeout + 30
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = server.wait_for_result(
                timeout=min(1.0, remaining))
        except queue.Empty:
            continue
        if msg.get("batch_id") != batch_id:
            continue
        ack = msg.get("__ack")
        if ack == "watch":
            continue
        if ack == "watch-done":
            try:
                outstanding_watches.remove(batch_id)
            except ValueError:
                pass
            reason = msg.get("reason")
            if reason == "settled":
                print(f"  [ok]   watch (settled)", file=sys.stderr)
                return {
                    "success": True, "stage": "complete",
                    "reason": None,
                    "details": {"watch_reason": reason},
                }
            print(
                f"  [fail] watch: reason={reason!r}",
                file=sys.stderr,
            )
            return {
                "success": False, "stage": "watch",
                "reason": f"watch-done reason={reason!r}",
                "details": msg,
            }

    return {
        "success": False, "stage": "watch",
        "reason": f"watch timeout after {watch_timeout:.0f}s",
        "details": {},
    }


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------

def _send_part_with_retry(
    server,
    part: Path, idx: int, total: int, text: str, tab_id: int,
    *,
    retries: int,
    attach_timeout: float,
    watch_timeout: float,
    reply_timeout: float,
    min_reply_chars: int,
    no_wait: bool,
    use_watch: bool,
    ready_timeout: float,
    ready_poll: float,
    upload_timeout: float,
    upload_min_wait: float,
    enter_attempts: int,
    outstanding_watches: list[str],
) -> dict:
    last_result: Optional[dict] = None
    max_attempts = retries + 1
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            delay = _retry_delay(attempt)
            print(
                f"  [retry] attempt {attempt}/{max_attempts} "
                f"(after {delay:.0f}s delay)",
                file=sys.stderr,
            )
            if delay > 0:
                time.sleep(delay)
            if not _wait_for_composer_ready(
                server, tab_id,
                ready_timeout=ready_timeout,
                poll_interval=ready_poll,
            ):
                print(
                    f"  [retry] composer never became ready; "
                    f"attempting anyway",
                    file=sys.stderr,
                )
        result = _send_one_part(
            server, part, idx, total, text, tab_id,
            attach_timeout=attach_timeout,
            watch_timeout=watch_timeout,
            reply_timeout=reply_timeout,
            min_reply_chars=min_reply_chars,
            no_wait=no_wait,
            use_watch=use_watch,
            upload_timeout=upload_timeout,
            upload_min_wait=upload_min_wait,
            enter_attempts=enter_attempts,
            outstanding_watches=outstanding_watches,
        )
        result["attempts"] = attempt
        last_result = result
        if result["success"]:
            return result
        if result["stage"] in ("wait-reply", "watch"):
            return result

    return last_result or {
        "success": False, "stage": "internal",
        "reason": "no attempts made", "details": {}, "attempts": 0,
    }


def _needs_between_parts_wait(result: dict) -> bool:
    """True if we should probe before the next part.

    False when the reply-wait reported `is_generating == False` —
    the next attach's internal `waitComposerReady` is the gate.
    """
    details = result.get("details") or {}
    reply_wait = details.get("reply_wait") or {}
    return reply_wait.get("is_generating") is not False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    dump_path = args.file.resolve()
    if not dump_path.is_file():
        print(f"error: not a file: {dump_path}", file=sys.stderr)
        return 1

    # ---- argument validation ----
    explicit_modes = sum(
        1 for v in (args.parts, args.target_lines, args.max_part_bytes)
        if v is not None
    )
    if explicit_modes > 1:
        print(
            "error: -n/--parts, --target-lines, and --max-part-bytes "
            "are mutually exclusive",
            file=sys.stderr,
        )
        return 3
    if args.parts is not None and args.parts < 1:
        print("error: --parts must be >= 1", file=sys.stderr)
        return 3
    if args.target_lines is not None and args.target_lines < 1:
        print("error: --target-lines must be >= 1", file=sys.stderr)
        return 3
    if args.max_part_bytes is not None and args.max_part_bytes < 1:
        print("error: --max-part-bytes must be >= 1", file=sys.stderr)
        return 3
    if args.retries < 0:
        print("error: --retries must be >= 0", file=sys.stderr)
        return 3
    if args.upload_timeout <= 0:
        print("error: --upload-timeout must be > 0", file=sys.stderr)
        return 3
    if args.upload_min_wait < 0:
        print("error: --upload-min-wait must be >= 0", file=sys.stderr)
        return 3
    if args.enter_attempts < 1:
        print("error: --enter-attempts must be >= 1", file=sys.stderr)
        return 3
    if args.reply_timeout <= 0:
        print("error: --reply-timeout must be > 0", file=sys.stderr)
        return 3
    if args.min_reply_chars < 1:
        print("error: --min-reply-chars must be >= 1", file=sys.stderr)
        return 3
    if args.gap < 0:
        print("error: --gap must be >= 0", file=sys.stderr)
        return 3

    min_attach_timeout = max(args.upload_timeout + 20.0, 20.0)
    effective_attach_timeout = max(
        args.attach_timeout, min_attach_timeout,
    )
    if effective_attach_timeout > args.attach_timeout:
        print(
            f"[config] bumping attach_timeout from "
            f"{args.attach_timeout:.0f}s to "
            f"{effective_attach_timeout:.0f}s",
            file=sys.stderr,
        )

    # ---- workspace ----
    if args.workspace is None:
        ws = Path(tempfile.mkdtemp(prefix="deeppeek_split_send_"))
    else:
        ws = args.workspace.resolve()
        ws.mkdir(parents=True, exist_ok=True)
    print(f"[workspace] {ws}", file=sys.stderr)
    print(f"  (kept after run for inspection; rm -rf {ws} when done)",
          file=sys.stderr)

    input_sha = _sha256_of(dump_path)

    prior_progress: Optional[dict] = None
    if args.resume:
        prior_progress = _load_progress(ws, input_sha)
        if prior_progress is not None:
            completed = prior_progress.get("completed") or []
            print(
                f"[resume] {len(completed)} part(s) already completed: "
                f"{completed}",
                file=sys.stderr,
            )

    try:
        parts = _stage_split(
            dump_path, ws,
            n_parts=args.parts,
            target_lines=args.target_lines,
            max_part_bytes=args.max_part_bytes,
            timeout=args.split_timeout,
        )
    except SystemExit as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    total = len(parts)
    print(f"[split] {total} part(s)", file=sys.stderr)
    for p in parts:
        print(f"  {p.name}  ({p.stat().st_size:,} bytes)",
              file=sys.stderr)

    texts: list[str] = []
    for i, p in enumerate(parts, 1):
        try:
            t = args.text_template.format(i=i, n=total, name=p.name)
        except (KeyError, IndexError) as e:
            print(
                f"error: text template format failed: {e}\n"
                f"  available placeholders: {{i}}, {{n}}, {{name}}",
                file=sys.stderr,
            )
            return 3
        texts.append(t)

    if args.dry_run:
        for (p, t) in zip(parts, texts):
            print(f"{p.name}\t{t}")
        print(
            f"\n[dry-run] {total} part(s) would be sent "
            f"sequentially; nothing sent.",
            file=sys.stderr,
        )
        return 0

    completed: set[int] = set()
    if prior_progress is not None:
        for n in (prior_progress.get("completed") or []):
            try:
                completed.add(int(n))
            except (TypeError, ValueError):
                pass
    progress = {
        "input_sha256": input_sha,
        "input_path": str(dump_path),
        "total": total,
        "completed": sorted(completed),
        "current": None,
        "status": "starting",
        "started_at": int(time.time()),
    }
    _save_progress(ws, progress)

    with composer_session(
        port=args.port, timeout=args.connect_timeout
    ) as server:
        if server is None:
            return 1

        outstanding_watches: list[str] = []
        tab_id_for_cleanup: Optional[int] = None
        try:
            rc, tab_id = _stage_bind_tab(
                server, timeout=args.bind_timeout,
                explicit_tab_id=args.tab_id,
            )
            if rc != 0 or tab_id is None:
                return 1
            tab_id_for_cleanup = tab_id

            rc = _stage_bind_composer(
                server, tab_id, timeout=args.bind_timeout,
            )
            if rc != 0:
                return 1

            print(f"[ready] waiting for composer to be usable...",
                  file=sys.stderr)
            if not _wait_for_composer_ready(
                server, tab_id,
                ready_timeout=args.ready_timeout,
                poll_interval=args.ready_poll,
            ):
                print(
                    "error: composer never became ready at startup",
                    file=sys.stderr,
                )
                return 1
            print(f"[ready] composer ready", file=sys.stderr)

            failures: list[dict] = []
            for i, (part, text) in enumerate(zip(parts, texts), 1):
                if i in completed:
                    print(
                        f"\n[skip] part {i}/{total}: already completed "
                        f"(from progress.json)", file=sys.stderr,
                    )
                    continue

                progress["current"] = i
                progress["status"] = "sending"
                _save_progress(ws, progress)

                result = _send_part_with_retry(
                    server, part, i, total, text, tab_id,
                    retries=args.retries,
                    attach_timeout=effective_attach_timeout,
                    watch_timeout=args.watch_timeout,
                    reply_timeout=args.reply_timeout,
                    min_reply_chars=args.min_reply_chars,
                    no_wait=args.no_wait,
                    use_watch=args.use_watch,
                    ready_timeout=args.ready_timeout,
                    ready_poll=args.ready_poll,
                    upload_timeout=args.upload_timeout,
                    upload_min_wait=args.upload_min_wait,
                    enter_attempts=args.enter_attempts,
                    outstanding_watches=outstanding_watches,
                )

                if not result["success"]:
                    failure_record = {
                        "part": i,
                        "name": part.name,
                        "stage": result.get("stage"),
                        "reason": result.get("reason"),
                        "attempts": result.get("attempts", 1),
                        "details": result.get("details"),
                    }
                    failures.append(failure_record)
                    progress["status"] = "failed"
                    progress["last_failure"] = failure_record
                    _save_progress(ws, progress)
                    print(
                        f"\nerror: part {i}/{total} failed "
                        f"after {result.get('attempts', 1)} attempt(s)\n"
                        f"  stage:  {result.get('stage')!r}\n"
                        f"  reason: {result.get('reason')!r}",
                        file=sys.stderr,
                    )
                    details = result.get("details") or {}
                    for k in ("focus_ok", "focus_attempts",
                              "active_at_enter", "composer_source",
                              "composer_cleared",
                              "new_message_appeared",
                              "is_generating",
                              "msg_count_before", "msg_count_after",
                              "attempts_used"):
                        if k in details:
                            print(f"    {k}: {details[k]}",
                                  file=sys.stderr)
                    if details.get("attach_upload_wait"):
                        print(
                            f"    attach_upload_wait: "
                            f"{details['attach_upload_wait']}",
                            file=sys.stderr,
                        )
                    if not args.continue_on_error:
                        break
                else:
                    completed.add(i)
                    progress["completed"] = sorted(completed)
                    progress["current"] = None
                    progress["status"] = "sending"
                    _save_progress(ws, progress)

                if i < total and not args.no_wait:
                    if _needs_between_parts_wait(result):
                        if not _wait_for_composer_ready(
                            server, tab_id,
                            ready_timeout=args.ready_timeout,
                            poll_interval=args.ready_poll,
                        ):
                            print(
                                f"  [ready] composer did not become ready "
                                f"within {args.ready_timeout:.0f}s; "
                                f"proceeding anyway",
                                file=sys.stderr,
                            )
                    if args.gap > 0:
                        time.sleep(args.gap)

            progress["status"] = "done" if not failures else "partial"
            progress["finished_at"] = int(time.time())
            _save_progress(ws, progress)

            print(file=sys.stderr)
            if not failures:
                print(f"[done] all {total} part(s) sent.",
                      file=sys.stderr)
                return 0

            print(
                f"[done] {len(failures)} of {total} part(s) failed:",
                file=sys.stderr,
            )
            for f in failures:
                print(
                    f"  part {f['part']}/{total}: {f['name']}\n"
                    f"    stage:  {f['stage']!r}\n"
                    f"    reason: {f['reason']!r}\n"
                    f"    attempts: {f['attempts']}",
                    file=sys.stderr,
                )
            print(
                f"\nprogress: {ws / PROGRESS_FILE_NAME}\n"
                f"  resumable with `--workspace {ws} --resume`",
                file=sys.stderr,
            )
            return 2
        finally:
            if outstanding_watches:
                print(
                    f"[cleanup] stopping {len(outstanding_watches)} "
                    f"outstanding watch(es)",
                    file=sys.stderr,
                )
                for bid in outstanding_watches:
                    try:
                        payload = {"batch_id": bid}
                        if tab_id_for_cleanup is not None:
                            payload["tab_id"] = tab_id_for_cleanup
                        server._broadcast("stop-watch", payload)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "split_send",
        help="Split a dump file into parts and send each part "
             "sequentially to DeepSeek with a short ack prompt.  "
             "By default uses fit-parts mode (each part under "
             "125 KiB); override with -n, --target-lines, or "
             "--max-part-bytes.",
    )
    p.add_argument(
        "file", type=Path, metavar="FILE",
        help="dump file to split and send",
    )

    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-n", "--parts", type=int, default=None, metavar="N",
        help="explicit number of parts (overrides fit mode).",
    )
    mode_group.add_argument(
        "--target-lines", type=int, default=None, metavar="L",
        help="target L lines per part (overrides fit mode).",
    )
    mode_group.add_argument(
        "--max-part-bytes", type=int, default=None, metavar="N",
        help="fit mode with a custom byte limit.  Default: the "
             "constant DEFAULT_MAX_PART_BYTES in dumps_split.py "
             "(125 KiB).  Parts are iteratively recomputed with "
             "more splits until every part is smaller than N.",
    )

    p.add_argument(
        "-t", "--text-template", default=DEFAULT_PROMPT_TEMPLATE,
        metavar="STR",
        help=f"composer text per part.  {{i}}, {{n}}, {{name}} "
             f"(default: {DEFAULT_PROMPT_TEMPLATE!r})",
    )
    p.add_argument(
        "--tab-id", type=int, default=None, metavar="N",
        help="pin to this tab id instead of auto-discovering.",
    )
    p.add_argument(
        "--workspace", type=Path, default=None,
        help="directory for the split parts and progress.json.",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="skip parts already marked completed in progress.json.",
    )
    p.add_argument(
        "--reply-timeout", type=float, default=60.0, metavar="SECONDS",
        help="max wait for the assistant's reply to appear in the "
             "DOM (default: 60).",
    )
    p.add_argument(
        "--min-reply-chars", type=int, default=1, metavar="N",
        help="minimum reply length before next part is sent "
             "(default: 1).",
    )
    p.add_argument(
        "--use-watch", action="store_true",
        help="use the full watch flow (Stop button + API extraction) "
             "instead of wait-for-reply.",
    )
    p.add_argument(
        "--enter-attempts", type=int, default=5, metavar="N",
        help="how many times doEnter presses Enter before giving up "
             "(default: 5).",
    )
    p.add_argument(
        "--retries", type=int, default=1, metavar="N",
        help="pipeline-level retries on failure (default: 1).",
    )
    p.add_argument(
        "--continue-on-error", action="store_true",
        help="continue sending subsequent parts after a failure.",
    )
    p.add_argument(
        "--no-wait", action="store_true",
        help="skip the reply-wait step entirely.",
    )
    p.add_argument(
        "--upload-timeout", type=float, default=30.0, metavar="SECONDS",
        help="max wait for a file chip to finish uploading "
             "(default: 30).",
    )
    p.add_argument(
        "--upload-min-wait", type=float, default=0.5, metavar="SECONDS",
        help="minimum wait after a chip appears (default: 0.5).",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="SSE port (default: 8765)",
    )
    p.add_argument(
        "--connect-timeout", type=float, default=120.0,
        help="initial extension-connection timeout (default: 120)",
    )
    p.add_argument(
        "--bind-timeout", type=float, default=30.0,
        help="timeout for bind-tab / bind-composer (default: 30)",
    )
    p.add_argument(
        "--attach-timeout", type=float, default=60.0,
        help="per-event timeout for clear / type / enter "
             "(default: 60).",
    )
    p.add_argument(
        "--watch-timeout", type=float, default=300.0,
        help="max wait per part when --use-watch is set "
             "(default: 300).",
    )
    p.add_argument(
        "--ready-timeout", type=float, default=30.0,
        help="how long to poll for composer readiness (default: 30).",
    )
    p.add_argument(
        "--ready-poll", type=float, default=0.2,
        help="polling interval for the readiness probe "
             "(default: 0.2).",
    )
    p.add_argument(
        "--split-timeout", type=float, default=300.0,
        help="timeout for dumps_split.py (default: 300)",
    )
    p.add_argument(
        "--gap", type=float, default=0.0,
        help="minimum seconds between parts after readiness settles "
             "(default: 0.0).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="split but don't send; print the plan and exit.",
    )
    p.set_defaults(func=run)