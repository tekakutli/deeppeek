"""`deeppeek prompt retry` — reset composer with the last prompt's setup.

Finds the most recent user prompt and recreates the composer state
that produced it:

  1. clears the composer (removes any existing file chips and text)
  2. re-attaches the same files (if any)
  3. retypes the original prompt text

The composer is left ready to submit — press Enter in the browser
when you're satisfied, or edit the text first.

TWO SOURCES FOR "THE LAST PROMPT"
=================================
Retry checks two places for the most recent user message:

  1. **Pending fiber state** (preferred).  Messages the client is
     still rendering but hasn't written to IndexedDB.  This is
     where a failed submission lives: the file was uploaded
     (status: SUCCESS on each file record) and the message was
     rendered optimistically, but the send POST failed, so
     nothing reached the transcript.  Reloading the tab destroys
     this state.  Retry can recover the prompt text and
     re-attach the files — but only before reload.

  2. **IndexedDB history-message** (fallback).  Persisted
     messages.  Always available; survives reload.

The CLI reports which source was used:

    source=pending-fiber   recovered a message not yet in the
                           transcript (failed submission)
    source=indexeddb       normal retry of the persisted last
                           user message

Message selection is "the last user message, period" — not "the
last user message with files".  Filtering for prompts that had
files surprises the user by picking a much older message whenever
the tail has no attachments.

Use `--message-id` to override with a specific persisted message.
That path always reads from IndexedDB (fiber messages have no
stable id, so they cannot be addressed by --message-id).

Exit codes:
    0  composer reset successfully
    1  extension connection failure
    2  reset failed (attach threw, type threw, or no target found)
    3  argument error
"""

from __future__ import annotations

import argparse
import sys

from ..commands._composer import broadcast_and_wait, composer_session


DEFAULT_TIMEOUT = 300.0


def run(args: argparse.Namespace) -> int:
    opts: dict = {"include_forked": bool(args.include_forked)}
    if args.message_id is not None:
        opts["message_id"] = int(args.message_id)

    print(f"[connect] waiting for extension on port {args.port}...",
          file=sys.stderr)

    with composer_session(port=args.port, timeout=args.timeout) as server:
        if server is None:
            return 1

        print("[retry] resetting composer from last prompt setup...",
              file=sys.stderr)

        rc, result = broadcast_and_wait(
            server, "retry-last", {"opts": opts},
            timeout=args.timeout, ack_name="retry-last",
        )
        if rc != 0:
            reason = (result.get("reason") or result.get("error")
                      or "(no reason)")
            print(f"error: retry failed: {reason}", file=sys.stderr)
            return 2

    if not result.get("success"):
        reason = result.get("reason") or "(no reason)"
        print(f"error: {reason}", file=sys.stderr)
        mid = result.get("message_id")
        if mid is not None:
            print(f"  msg_id={mid}", file=sys.stderr)
        return 2

    mid = result.get("message_id")
    source = result.get("source", "(unknown)")
    n_total = result.get("file_count", 0)
    n_attached = result.get("files_attached", 0)
    n_fetched = result.get("files_fetched", n_attached)
    text_len = result.get("text_length", 0)

    print(f"\n[retry] source={source}  msg_id={mid}", file=sys.stderr)
    if source == "pending-fiber":
        print("  (recovered from in-memory state — not in IndexedDB;"
              " reload will destroy this recovery)", file=sys.stderr)
    print(f"  files: {n_attached}/{n_total} attached"
          + (f"  ({n_fetched} fetched, {n_attached} accepted by composer)"
             if n_fetched != n_attached else ""),
          file=sys.stderr)
    print(f"  text:  {text_len} chars", file=sys.stderr)

    errors = result.get("errors") or []
    if errors:
        print(f"\n  {len(errors)} file(s) failed to fetch:", file=sys.stderr)
        for e in errors:
            print(f"    {e.get('name', '(unnamed)')}: "
                  f"{e.get('error', 'unknown')}", file=sys.stderr)

    type_result = result.get("type_result") or {}
    if type_result and not type_result.get("success"):
        print(f"\n  type failed: "
              f"{type_result.get('reason', 'unknown')}", file=sys.stderr)
        return 2

    print(f"\n[done] composer reset — press Enter in the browser "
          f"to send", file=sys.stderr)
    if errors:
        return 2
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "retry",
        help="Reset the composer with the last prompt: clears, "
             "re-attaches the files, retypes the text.  Recovers "
             "failed submissions from in-memory state before they "
             "are lost on reload.",
    )
    p.add_argument(
        "--message-id", type=int, default=None, metavar="ID",
        help="reset from a specific persisted message (default: "
             "the most recent user message, preferring pending "
             "in-memory state over IndexedDB)",
    )
    p.add_argument(
        "--include-forked", action="store_true",
        help="consider forked-away branches when searching "
             "IndexedDB for the last user message",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="extension SSE port (default: 8765)",
    )
    p.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"connection + per-file timeout in seconds "
             f"(default: {DEFAULT_TIMEOUT:.0f})",
    )
    p.set_defaults(func=run)
