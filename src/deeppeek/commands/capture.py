"""`deeppeek capture` — capture a DeepSeek conversation.

Modes:

  capture                    recover in memory → ./recovered/
  capture --apply-to DIR     recover in memory → applied directly to DIR
  capture -o chat.md         write chat.md only (old behavior)
  capture -o chat.md \
      --recover              write chat.md AND recover
  capture --json -o chat.md  write JSON, no recovery

The extension watchdog is controlled by `deeppeek extension watchdog
on/off`, not by capture flags.
"""
import argparse
import sys
from pathlib import Path

from ..capture.service import CaptureConfig, capture_conversation
from ..recover.pipeline import RecoverConfig, run as recover_run


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "capture",
        help="Capture a DeepSeek conversation "
             "(default: recover its files in memory)",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=None,
        help="write the markdown to this path "
             "(skips recovery unless --recover or --apply-to is given)",
    )
    p.add_argument(
        "--recover", action="store_true",
        help="force recovery even when -o is given",
    )
    p.add_argument(
        "--no-recover", action="store_true",
        help="skip recovery (requires -o; nothing would be saved otherwise)",
    )
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
             "PATTERN (Python regex), slice to end.  Overrides --last.",
    )
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--no-hints", action="store_true")
    p.add_argument("--latex", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=None)

    # ---- recovery-side flags ----
    p.add_argument("--recover-to", type=Path, default=None)
    p.add_argument("--apply-to", type=Path, default=None, metavar="DIR")
    p.add_argument("--layout", choices=["flat", "header", "passthrough"],
                   default="flat")
    p.add_argument("--remap", action="append", default=[], metavar="OLD=NEW")
    p.add_argument("--min-trust", choices=["HIGH", "MEDIUM", "LOW"],
                   default="MEDIUM")
    p.add_argument("--llm-mode", choices=["off", "smart", "always"],
                   default="off")
    p.add_argument("--llm-url", default=None)
    p.add_argument("--llm-model", default=None)
    p.add_argument("--llm-timeout", type=int, default=None)
    p.add_argument("--escalate-below", type=float, default=None)
    p.add_argument("--llm-max-calls", type=int, default=None)
    p.add_argument("--no-llm-ping", dest="llm_ping", action="store_false")
    p.add_argument("--cache-dir", type=Path, default=None)
    p.add_argument("--trace", action="store_true")
    p.set_defaults(func=run)


def _parse_remap(items):
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"error: bad --remap {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def run(args: argparse.Namespace) -> int:
    if args.apply_to is not None and args.recover_to is not None:
        print("error: --apply-to and --recover-to are mutually exclusive",
              file=sys.stderr)
        return 3
    if args.no_recover and args.apply_to is not None:
        print("error: --no-recover and --apply-to are mutually exclusive "
              "(--apply-to implies recovery)", file=sys.stderr)
        return 3

    if args.no_recover:
        do_recover = False
    elif args.apply_to is not None:
        do_recover = True
    elif args.recover:
        do_recover = True
    elif args.output:
        do_recover = False
    else:
        do_recover = True

    if not do_recover and not args.output:
        print("error: --no-recover requires -o/--output",
              file=sys.stderr)
        return 3
    if args.json and do_recover:
        print("note: --json disables recovery", file=sys.stderr)
        do_recover = False
        if not args.output:
            print("error: --json alone would discard the capture — "
                  "add -o to write it somewhere", file=sys.stderr)
            return 3

    try:
        result = capture_conversation(CaptureConfig(
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
        ))
    except KeyboardInterrupt:
        return 1

    if args.output:
        args.output.write_text(result.text, encoding="utf-8")
        print(f"Wrote {args.output} ({result.message_count} message(s))",
              file=sys.stderr)

    if not do_recover:
        return 0

    out_dir = args.recover_to if args.recover_to is not None else Path("./recovered")

    return recover_run(RecoverConfig(
        dump_text=result.text,
        out_dir=out_dir,
        apply_to=args.apply_to,
        layout=args.layout,
        remap=_parse_remap(args.remap),
        min_trust=args.min_trust,
        llm_mode=args.llm_mode,
        llm_url=args.llm_url,
        llm_model=args.llm_model,
        llm_timeout=args.llm_timeout,
        escalate_below=args.escalate_below,
        llm_max_calls=args.llm_max_calls,
        llm_ping=args.llm_ping,
        cache_dir=args.cache_dir,
        verbose=getattr(args, "verbose", False),
        trace=args.trace,
    ))
