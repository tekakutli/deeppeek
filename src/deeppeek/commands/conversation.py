"""`deeppeek conversation` — capture a DeepSeek conversation to markdown.

The minimal capture path: retrieve the conversation from the browser
extension, slice it, write it to disk.  Nothing else — no recovery,
no apply-to, no LLM, no metadata directory.

Distinct from `capture`, which runs recovery by default and accepts
a large set of recovery-side flags.  `conversation` exists for the
cases where the markdown IS the deliverable: archiving a transcript,
reading it, or piping it into another tool.

Shared flags (`--last`, `--last-regex`, `--start-prompt`, slicer,
`--no-thinking`, `--latex`, `--json`, …) come from the same helpers
that `capture` uses, so the two commands cannot drift.

    deeppeek conversation                # → ./conversation.md
    deeppeek conversation --last 3       # last 3 user turns only
    deeppeek conversation -o chat.md     # → ./chat.md
    deeppeek conversation -o /tmp/x.md --json
"""
import argparse
import sys
from pathlib import Path

from ..capture.service import capture_conversation
from .capture import add_capture_side_args, capture_config_from_args


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
    add_capture_side_args(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    result = capture_conversation(capture_config_from_args(args))

    output = args.output
    if output is None:
        output = Path("conversation.json" if args.json else "conversation.md")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.text, encoding="utf-8")
    print(f"Wrote {output} ({result.message_count} message(s))",
          file=sys.stderr)
    return 0
