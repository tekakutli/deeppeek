"""`deeppeek conversation` — capture a DeepSeek conversation to markdown.

The minimal capture path: retrieve the conversation from the browser
extension, slice it, write it to disk.  Nothing else — no recovery,
no apply-to, no LLM, no metadata directory.

    deeppeek conversation                # → ./conversation.md
    deeppeek conversation --last 3       # last 3 user turns only
    deeppeek conversation -o chat.md     # → ./chat.md
    deeppeek conversation -o /tmp/x.md --json

The capture-side flag set (`--last`, `--last-regex`, `--start-prompt`,
`--until`, `--until-regex`, `--no-thinking`, `--latex`, `--json`, …)
lives in `_add_capture_side_args` / `_capture_config_from_args` below,
inlined from the now-removed `commands/capture.py`.
"""
import argparse
import sys
from pathlib import Path

from ..conversation.service import (
    CaptureConfig,
    capture_conversation,
)


def _add_capture_side_args(p: argparse.ArgumentParser) -> None:
    """Capture-side arguments shared across the conversation-shaped
    commands.  `-o/--output` is NOT included — callers define it
    themselves to control auto-naming vs path semantics."""
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--watch", action="store_true")
    p.add_argument("--start-prompt")
    p.add_argument("--end-prompt")
    p.add_argument("--match-mode",
                   choices=["substring", "exact", "regex"],
                   default="substring")
    p.add_argument("--start-index", type=int)
    p.add_argument("--end-index", type=int)
    p.add_argument("--context", type=int, default=0)
    p.add_argument(
        "--last", type=int, default=None, metavar="N",
        help="keep only the last N user turns.",
    )
    p.add_argument(
        "--last-regex", default=None, metavar="PATTERN",
        help="anchor at the LAST user prompt whose content matches "
             "PATTERN (Python regex), slice to end.  The very "
             "last matching turn overall — implemented as a "
             "BACKWARD scan from the conversation end (first hit "
             "wins).  Mirror of --until-regex's forward scan.  "
             "Overrides --last.",
    )
    p.add_argument(
        "--until", type=int, default=None, metavar="K",
        help="end the --last/--last-regex slice JUST BEFORE user "
             "turn K (0-indexed from conversation start): keeps "
             "user turns [start, K-1], exclusive of turn K.  "
             "Requires --last N or --last-regex.  K must be "
             "strictly greater than the slice's start user-turn "
             "index.  Mutually exclusive with --until-regex.",
    )
    p.add_argument(
        "--until-regex", default=None, metavar="PATTERN",
        help="end the --last/--last-regex slice JUST BEFORE the "
             "FIRST user prompt at or after the slice start "
             "whose content matches PATTERN.  Scans forward from "
             "the slice start and takes the very next match — "
             "NOT the last match in the conversation.  Requires "
             "--last N or --last-regex.  Errors if the first "
             "match is the slice start itself (excluding it "
             "would leave no turns).  Mutually exclusive with "
             "--until.",
    )
    p.add_argument(
        "--include-forked", action="store_true",
        help="include forked conversation branches in the captured "
             "messages.  DeepSeek stores the conversation as a "
             "message tree; editing an earlier prompt (or "
             "regenerating a reply) forks it.  By default only the "
             "active path — the messages the UI is actually "
             "rendering — is captured.  Pass this to also include "
             "every hidden branch that lives in IndexedDB.  Useful "
             "for auditing or recovering discarded work; harmful "
             "for normal capture, since forked messages look like "
             "context that never appeared on screen.",
    )
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--no-hints", action="store_true")
    p.add_argument("--latex", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of Markdown")
    p.add_argument("--timeout", type=float, default=None)


def _capture_config_from_args(args: argparse.Namespace) -> CaptureConfig:
    """Build a CaptureConfig from the shared capture-side args.

    `output` is always None: the caller writes the returned text to
    disk itself.  `capture_conversation` does not write.
    """
    return CaptureConfig(
        port=args.port,
        watch=args.watch,
        output=None,
        start_prompt=args.start_prompt,
        end_prompt=args.end_prompt,
        start_index=args.start_index,
        end_index=args.end_index,
        match_mode=args.match_mode,
        context=args.context,
        include_thinking=not args.no_thinking,
        include_hints=not args.no_hints,
        latex=args.latex,
        as_json=args.json,
        timeout=args.timeout,
        last_user_turns=args.last,
        last_regex=args.last_regex,
        until_user_turn=args.until,
        until_regex=args.until_regex,
        include_forked=args.include_forked,
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "conversation",
        help="Capture a DeepSeek conversation to markdown (no recovery)",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="write the conversation to PATH.  Default: "
             "conversation.md in the current directory "
             "(conversation.json when --json is given).",
    )
    _add_capture_side_args(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    result = capture_conversation(_capture_config_from_args(args))

    output = args.output
    if output is None:
        output = Path("conversation.json" if args.json else "conversation.md")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.text, encoding="utf-8")
    print(f"Wrote {output} ({result.message_count} message(s))",
          file=sys.stderr)

    if result.message_count == 0:
        print(
            "warning: no messages were captured.  Common causes:\n"
            "  · the extension couldn't read the conversation — "
            "reload the deepseek tab (F5) and retry\n"
            "  · the conversation is still generating — wait for "
            "it to finish, then retry\n"
            "  · the extension needs updating — run "
            "`deeppeek extension status` and `deeppeek diagnose`",
            file=sys.stderr,
        )
    return 0
