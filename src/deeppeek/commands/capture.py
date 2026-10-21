"""`deeppeek capture` — run the local server, save the conversation."""
import argparse
from pathlib import Path

from ..capture.service import CaptureConfig, capture_to_markdown


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("capture", help="Capture a DeepSeek conversation from the browser")
    p.add_argument("-o", "--output", type=Path, default=None)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--watch", action="store_true")
    p.add_argument("--start-prompt")
    p.add_argument("--end-prompt")
    p.add_argument("--match-mode", choices=["substring", "exact", "regex"], default="substring")
    p.add_argument("--start-index", type=int)
    p.add_argument("--end-index", type=int)
    p.add_argument("--context", type=int, default=0)
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--no-hints", action="store_true")
    p.add_argument("--latex", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        out = capture_to_markdown(CaptureConfig(
            port=args.port,
            watch=args.watch,
            output=args.output,
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
        ))
    except KeyboardInterrupt:
        return 1
    return 0
