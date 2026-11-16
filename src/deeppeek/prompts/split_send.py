"""`deeppeek prompt split_send` — split a dump and send parts sequentially.

The purpose: load a large markdown dump into DeepSeek's conversation
context across multiple turns.  Each part is attached as a file chip,
followed by a short acknowledgment prompt ("this is part i/N, just say
roger").  The model's reply is expected to be brief so the assistant
side doesn't burn context; over N turns, the full dump content
accumulates in the conversation history.

Pipeline:

    split   `dumps_split.py <file> -n N -o <ws>`
              (uses the sibling script under scripts/dumps/; the
               direct invocation avoids paying the `deeppeek scripts`
               dispatch cost on every run)
    send    for each part i/N:
              attach   file chip via the `attach` SSE event
              type     "this is part i/N, just say roger" via `type`
              enter    submit via `enter`
              watch    wait for the model to finish via `watch`
                       (skipped with --no-wait; unsafe)

The `watch` step is what serializes the pipeline.  DeepSeek's composer
is disabled while a response is generating; if we didn't wait, part
N+1's `attach` could fire during part N's generation and either fail
(drop rejected) or be silently appended to N's attachments.  `watch`
waits for the "Stop" button to disappear (generation finished) before
returning, and only then does the next part begin.

Usage:

    deeppeek prompt split_send /path/to/dump.md
    deeppeek prompt split_send dump.md --parts 5
    deeppeek prompt split_send dump.md --target-lines 3000
    deeppeek prompt split_send dump.md --dry-run
    deeppeek prompt split_send dump.md -t "part {i}/{n} — reply with ok"
    deeppeek prompt split_send dump.md --no-wait    # fire-and-forget

Default parts = 9, matching `dumps_split.py`'s `DEFAULT_NUM_PARTS`.

Failure model: on a per-part failure, the default is to bail
immediately (the tree is left as-is for inspection).  `--continue-on-error`
keeps going, which is useful when a single part transiently fails but
the extension is still healthy.
"""

from __future__ import annotations

import argparse
import base64
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
DEFAULT_PARTS = 9


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
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Part filename parsing
# ---------------------------------------------------------------------------

def _part_number(path: Path) -> Optional[int]:
    """Return the part number from `<stem>_part<i>of<N><suffix>`, or
    None if `path` doesn't match that pattern.

    `dumps_split.py` writes names matching `<dump_stem>_part<i>of<N>
    <dump_suffix>`.  We control the workspace, so the only files
    present should be parts; the pattern check keeps unrelated files
    out if the user passes a shared `--workspace` dir.
    """
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
# Split stage
# ---------------------------------------------------------------------------

def _stage_split(
    dump_path: Path,
    ws: Path,
    n_parts: Optional[int],
    target_lines: Optional[int],
    timeout: float,
) -> list[Path]:
    """Run `dumps_split.py` and return the resulting part paths,
    sorted by part number.

    `-o ws` keeps the parts out of the user's cwd; the default naming
    is `<stem>_part<i>of<N><suffix>`, which we parse back out.
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
    if target_lines is not None:
        cmd.extend(["--target-lines", str(target_lines)])
    else:
        n = n_parts if n_parts is not None else DEFAULT_PARTS
        cmd.extend(["-n", str(n)])
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
# Send stage
# ---------------------------------------------------------------------------

def _send_one_part(
    server,
    part_path: Path,
    idx: int,
    total: int,
    text: str,
    *,
    attach_timeout: float,
    watch_timeout: float,
    no_wait: bool,
) -> tuple[int, dict]:
    """Attach + type + enter + (optionally) watch for one part.

    Return-value convention matches `broadcast_and_wait`:
      0 = success
      1 = infrastructure failure (timeout, malformed ack)
      2 = command failure (extension reported success:false)
    """
    print(
        f"\n[send] part {idx}/{total}: {part_path.name} "
        f"({part_path.stat().st_size:,} bytes)",
        file=sys.stderr,
    )

    # ---- attach + type in parallel ----------------------------------
    #
    # Both events are independent — `attach` adds file chips,
    # `type` sets the textarea — so firing them back-to-back on
    # one connection halves the per-part round-trip count vs.
    # sequential.  `broadcast_many` returns only when both acks
    # arrive, so after this block both are confirmed.
    data = part_path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    specs = [
        {
            "tag": "attach",
            "event": "attach",
            "payload": {
                "files": [{
                    "name": part_path.name,
                    "mime": "text/markdown",
                    "base64": b64,
                }],
                "opts": {"delay_ms": 0},
            },
            "ack_name": "attach",
        },
        {
            "tag": "type",
            "event": "type",
            "payload": {"text": text},
            "ack_name": "type",
        },
    ]
    results = broadcast_many(server, specs, timeout=attach_timeout)
    for tag in ("attach", "type"):
        r = results.get(tag, {"exit_code": 1, "result": {}})
        if r["exit_code"] != 0:
            reason = (r["result"].get("reason")
                      or r["result"].get("error")
                      or "(no reason)")
            print(f"  [fail] {tag}: {reason}", file=sys.stderr)
            return r["exit_code"], {
                "stage": tag, "result": r["result"],
            }
        print(f"  [ok]   {tag}", file=sys.stderr)

    # ---- enter ------------------------------------------------------
    #
    # `enter` fires the key events and verifies that either the
    # composer cleared OR a new message appeared — either one is
    # proof the submit reached DeepSeek's React handler.
    rc, result = broadcast_and_wait(
        server, "enter", {},
        timeout=attach_timeout, ack_name="enter",
    )
    if rc != 0:
        reason = (result.get("reason")
                  or result.get("error")
                  or "(no reason)")
        print(f"  [fail] enter: {reason}", file=sys.stderr)
        return rc, {"stage": "enter", "result": result}
    print(f"  [ok]   enter", file=sys.stderr)

    if no_wait:
        print(f"  [skip] watch (--no-wait)", file=sys.stderr)
        return 0, {"stage": "complete", "reason": "no-wait"}

    # ---- watch ------------------------------------------------------
    #
    # `watch` waits for the "Stop" button to disappear (generation
    # finished) and emits the final content as a `watch-delta`
    # followed by `watch-done`.  We don't care about the content —
    # only that the model has finished, so the composer is
    # re-enabled and part N+1 can be attached without colliding.
    print(f"  [watch] waiting for model to finish...", file=sys.stderr)
    batch_id = uuid.uuid4().hex[:12]
    server._broadcast("watch", {
        "batch_id": batch_id,
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
            # Start ack — keep waiting for watch-done.
            continue
        if ack == "watch-done":
            reason = msg.get("reason")
            if reason == "settled":
                print(f"  [ok]   watch (settled)", file=sys.stderr)
                return 0, {"stage": "watch", "reason": reason}
            print(
                f"  [fail] watch: reason={reason!r}",
                file=sys.stderr,
            )
            return 2, {"stage": "watch", "reason": reason}
        # watch-delta / watch-rewrite — ignore content.

    return 1, {
        "stage": "watch",
        "error": f"watch timeout after {watch_timeout:.0f}s",
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    dump_path = args.file.resolve()
    if not dump_path.is_file():
        print(f"error: not a file: {dump_path}", file=sys.stderr)
        return 1

    # Argument validation (before any resources are created).
    if args.parts is not None and args.target_lines is not None:
        print(
            "error: --parts and --target-lines are mutually exclusive",
            file=sys.stderr,
        )
        return 3
    if args.parts is not None and args.parts < 1:
        print("error: --parts must be >= 1", file=sys.stderr)
        return 3
    if args.target_lines is not None and args.target_lines < 1:
        print("error: --target-lines must be >= 1", file=sys.stderr)
        return 3

    # ---- workspace --------------------------------------------------
    if args.workspace is None:
        ws = Path(tempfile.mkdtemp(prefix="deeppeek_split_send_"))
    else:
        ws = args.workspace.resolve()
        ws.mkdir(parents=True, exist_ok=True)
    print(f"[workspace] {ws}", file=sys.stderr)
    print(f"  (kept after run for inspection; rm -rf {ws} when done)",
          file=sys.stderr)

    # ---- split ------------------------------------------------------
    try:
        parts = _stage_split(
            dump_path, ws,
            n_parts=args.parts,
            target_lines=args.target_lines,
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

    # ---- per-part prompt text --------------------------------------
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

    # ---- dry-run ----------------------------------------------------
    if args.dry_run:
        for (p, t) in zip(parts, texts):
            print(f"{p.name}\t{t}")
        print(
            f"\n[dry-run] {total} part(s) would be sent "
            f"sequentially; nothing sent.",
            file=sys.stderr,
        )
        return 0

    # ---- send -------------------------------------------------------
    with composer_session(
        port=args.port, timeout=args.connect_timeout
    ) as server:
        if server is None:
            return 1

        failures: list[tuple[int, str, dict]] = []
        for i, (part, text) in enumerate(zip(parts, texts), 1):
            rc, result = _send_one_part(
                server, part, i, total, text,
                attach_timeout=args.attach_timeout,
                watch_timeout=args.watch_timeout,
                no_wait=args.no_wait,
            )
            if rc != 0:
                failures.append((i, part.name, result))
                if not args.continue_on_error:
                    print(
                        f"\nerror: part {i}/{total} failed "
                        f"(stage={result.get('stage')!r}, "
                        f"reason={result.get('reason') or result.get('error')!r})",
                        file=sys.stderr,
                    )
                    break
            elif i < total and args.gap > 0:
                # Composer needs a beat to re-enable after the
                # model's response completes; without this the
                # very next attach can land while the textarea
                # is still read-only.
                time.sleep(args.gap)

        print(file=sys.stderr)
        if not failures:
            print(f"[done] all {total} part(s) sent.", file=sys.stderr)
            return 0
        print(
            f"[done] {len(failures)} of {total} part(s) failed:",
            file=sys.stderr,
        )
        for (i, name, result) in failures:
            print(
                f"  part {i}/{total}: {name}  "
                f"stage={result.get('stage')!r}  "
                f"reason={result.get('reason') or result.get('error')!r}",
                file=sys.stderr,
            )
        return 2


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "split_send",
        help="Split a dump file into N parts and send each part "
             "sequentially to DeepSeek with a short ack prompt "
             "(\"this is part i/N, just say roger\")",
    )
    p.add_argument(
        "file", type=Path, metavar="FILE",
        help="dump file to split and send",
    )
    p.add_argument(
        "-n", "--parts", type=int, default=None, metavar="N",
        help=f"number of parts (default: {DEFAULT_PARTS}, matching "
             f"dumps_split.py).  Mutually exclusive with "
             f"--target-lines.",
    )
    p.add_argument(
        "--target-lines", type=int, default=None, metavar="L",
        help="alternative to --parts: target L lines per part.  "
             "Mutually exclusive with --parts.",
    )
    p.add_argument(
        "-t", "--text-template", default=DEFAULT_PROMPT_TEMPLATE,
        metavar="STR",
        help=f"composer text per part.  {{i}} is the 1-based part "
             f"index, {{n}} the total, {{name}} the part filename.  "
             f"(default: {DEFAULT_PROMPT_TEMPLATE!r})",
    )
    p.add_argument(
        "--workspace", type=Path, default=None,
        help="directory for the split parts.  Default: a fresh "
             "temp dir in /tmp.",
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
        "--attach-timeout", type=float, default=60.0,
        help="per-event timeout for attach / type / enter "
             "(default: 60)",
    )
    p.add_argument(
        "--watch-timeout", type=float, default=300.0,
        help="max wait per part for the model to finish generating "
             "(default: 300).  Raise this if the model routinely "
             "thinks longer than 5 minutes on each part.",
    )
    p.add_argument(
        "--split-timeout", type=float, default=300.0,
        help="timeout for dumps_split.py (default: 300)",
    )
    p.add_argument(
        "--gap", type=float, default=1.5,
        help="seconds between parts, giving the UI time to re-enable "
             "the composer (default: 1.5)",
    )
    p.add_argument(
        "--no-wait", action="store_true",
        help="skip the watch step — fire each part and move on "
             "without waiting for the model's reply.  Fast but "
             "risky; the next part may collide with the current "
             "generation.  Intended for testing.",
    )
    p.add_argument(
        "--continue-on-error", action="store_true",
        help="continue sending subsequent parts after a failure "
             "(default: bail on first failure, leaving the tree in "
             "place for inspection)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="split but don't send; print the plan (one line per "
             "part: filename TAB prompt text) and exit",
    )
    p.set_defaults(func=run)
