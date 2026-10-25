"""`deeppeek prompt last-thought` — extract the last Thought Process,
pipe it, and prefill the composer — in parallel.

Reads a conversation markdown, finds the LAST `### Thought Process`
section, discards everything above and below it (including any
post-thinking assistant content in the same message), then:

  1. pipes the section as a chat attachment (via the `attach` SSE
     event), and
  2. types a short follow-up into the composer (via the `type` SSE
     event),

on ONE `ExportServer` connection.  Both events are broadcast
back-to-back; the extension handles them concurrently.  The
sequence is therefore `max(pipe_time, type_time)`, not
`pipe_time + type_time`, and the text appears nearly simultaneously
with the file preview instead of a second later.

Earlier versions of the prompts spawned `deeppeek pipe` and
`deeppeek type` as subprocesses; each bound port 8765 in turn, so
the two stages were forced to serialize.  This module uses
`composer_session` + `broadcast_many` directly, which is what
`commands/_composer.py` was built for.

Usage:

    deeppeek prompt last-thought                  # extract, pipe, type (parallel)
    deeppeek prompt last-thought --dry-run        # print only
    deeppeek prompt last-thought --no-pipe        # type only
    deeppeek prompt last-thought --no-type        # pipe only
    deeppeek prompt last-thought --out ./t.md     # also write the extracted section
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path
from typing import Optional

from ..commands._composer import broadcast_many, composer_session


DEFAULT_CONVERSATION = Path("/tmp/conversation.md")
THOUGHT_HEADER = "### Thought Process"

DEFAULT_COMPOSER_TEXT = (
    "you had already thought about this, but you stopped at the end"
)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _extract_last_thought(text: str) -> Optional[str]:
    """Return the last Thought Process section, or None.

    A section is only accepted when the header is followed by a
    blank line and at least one `> `-quoted line — that rules out a
    `### Thought Process` string appearing inside assistant content
    or a code fence.

    Scanning is backwards (last -> first) so the most recent thinking
    wins.  Section content is exactly the header plus its quoted
    lines; everything after the last quoted line is dropped,
    including any post-thinking assistant content in the same
    message.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] != THOUGHT_HEADER:
            continue

        j = i + 1
        if j < len(lines) and lines[j] == "":
            j += 1
        if j >= len(lines) or not lines[j].startswith("> "):
            continue

        content = []
        while j < len(lines) and lines[j].startswith("> "):
            content.append(lines[j])
            j += 1

        return THOUGHT_HEADER + "\n\n" + "\n".join(content) + "\n"

    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    conv: Path = args.conversation
    if not conv.exists():
        print(f"error: conversation not found: {conv}", file=sys.stderr)
        return 1
    if not conv.is_file():
        print(f"error: not a regular file: {conv}", file=sys.stderr)
        return 1

    try:
        text = conv.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"error: cannot read {conv}: {e}", file=sys.stderr)
        return 1

    section = _extract_last_thought(text)
    if section is None:
        print(
            f"error: no '### Thought Process' section found in {conv}",
            file=sys.stderr,
        )
        return 1

    n_lines = len(section.splitlines())
    print(
        f"[extract] {n_lines} line(s), {len(section):,} chars",
        file=sys.stderr,
    )

    do_pipe = not args.no_pipe
    do_type = not args.no_type
    composer_text: str = args.text

    if not do_pipe and not do_type and args.out is None:
        print(
            "error: --no-pipe and --no-type together with no --out "
            "is a no-op (nothing to pipe, nothing to type, nowhere "
            "to write).",
            file=sys.stderr,
        )
        return 1
    if do_type and not composer_text.strip():
        print(
            "error: composer text is empty/whitespace-only.  Use "
            "--no-type to skip typing, or pass a non-empty --text.",
            file=sys.stderr,
        )
        return 1

    # Optional on-disk copy for inspection — independent of the pipe.
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(section, encoding="utf-8")
        print(f"[write] {args.out}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] extracted section below; not piped.",
              file=sys.stderr)
        print(section)
        if do_pipe:
            print(
                f"\n[dry-run] would pipe: "
                f"thought_process.md ({len(section):,} chars)",
                file=sys.stderr,
            )
        if do_type:
            print(
                f"[dry-run] would type: {composer_text!r}",
                file=sys.stderr,
            )
        return 0

    # ---- build the parallel event spec ----
    specs = []
    if do_pipe:
        b64 = base64.b64encode(section.encode("utf-8")).decode("ascii")
        specs.append({
            "tag": "pipe",
            "event": "attach",
            "payload": {
                "files": [{
                    "name": "thought_process.md",
                    "mime": "text/markdown",
                    "base64": b64,
                }],
                "opts": {"delay_ms": 0},
            },
            "ack_name": "attach",
        })
    if do_type:
        specs.append({
            "tag": "type",
            "event": "type",
            "payload": {"text": composer_text},
            "ack_name": "type",
        })

    # ---- one session, N concurrent events ----
    with composer_session(port=args.port, timeout=args.timeout) as server:
        if server is None:
            return 1

        print(f"[parallel] firing {len(specs)} event(s) on one "
              f"connection:", file=sys.stderr)
        for spec in specs:
            print(f"  {spec['event']} (tag={spec['tag']})",
                  file=sys.stderr)

        results = broadcast_many(server, specs, timeout=args.timeout)

        rc = 0
        for spec in specs:
            tag = spec["tag"]
            r = results.get(tag, {"exit_code": 1,
                                  "result": {"error": "no result"}})
            if r["exit_code"] == 0:
                print(f"  OK — {tag}", file=sys.stderr)
            else:
                reason = (
                    r["result"].get("reason")
                    or r["result"].get("error")
                    or "(no reason)"
                )
                print(f"  FAIL — {tag}: {reason}", file=sys.stderr)
                if rc == 0:
                    rc = r["exit_code"]
        return rc


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "last-thought",
        help="Extract the last '### Thought Process' section, pipe it, "
             "and type a follow-up into the composer — in parallel",
    )

    p.add_argument(
        "--conversation", type=Path, default=DEFAULT_CONVERSATION,
        help=f"conversation markdown to extract from "
             f"(default: {DEFAULT_CONVERSATION})",
    )
    p.add_argument(
        "--out", type=Path, default=None,
        help="also write the extracted section to this path (for "
             "inspection).  Independent of the pipe step.",
    )
    p.add_argument(
        "--text", default=DEFAULT_COMPOSER_TEXT,
        help=f"composer text to type after piping "
             f"(default: {DEFAULT_COMPOSER_TEXT!r})",
    )
    p.add_argument(
        "--no-pipe", action="store_true",
        help="extract and type the composer text, but don't pipe the "
             "section",
    )
    p.add_argument(
        "--no-type", action="store_true",
        help="extract and pipe the section, but don't type the "
             "composer text",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="print the extracted section and the composer text to "
             "stdout and stop (no pipe, no type)",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--timeout", type=float, default=60.0,
        help="seconds for extension connection AND each ack "
             "(default: 60)",
    )
    p.set_defaults(func=run)
