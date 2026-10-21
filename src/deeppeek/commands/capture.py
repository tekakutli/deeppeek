"""`deeppeek capture` — capture a DeepSeek conversation.

Modes:

  capture                    recover in memory → ./recovered/  (default)
  capture -o chat.md         write chat.md only (old behavior)
  capture -o chat.md \
      --recover              write chat.md AND recover in memory
  capture --no-recover \
      -o chat.md             same as `-o chat.md` (explicit form)
  capture --no-recover       ERROR (nothing to save)
  capture --json             print JSON, no recovery
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
        help="write the markdown to this path and skip recovery "
             "(use --recover to also run recovery)",
    )
    p.add_argument(
        "--recover", action="store_true",
        help="run recovery even when -o is given (write file AND recover)",
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
        help="keep only the last N user turns (each turn = 1 user "
             "message + its reply).  Overrides --start-prompt, "
             "--end-prompt, --start-index, --end-index.",
    )
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--no-hints", action="store_true")
    p.add_argument("--latex", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of Markdown (disables recovery)")
    p.add_argument("--timeout", type=float, default=None)

    # ---- recovery-side flags ----
    p.add_argument("--recover-to", type=Path, default=Path("./recovered"))
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
    # ---- decide recovery intent --------------------------------
    #  --no-recover                  → never recover
    #  -o file  (no --recover)       → file only, no recover
    #  -o file  --recover            → both
    #  no -o                         → recover (default)
    if args.no_recover:
        do_recover = False
    elif args.output and not args.recover:
        do_recover = False
    else:
        do_recover = True

    # ---- validate the flag combination -------------------------
    if not do_recover and not args.output:
        print("error: --no-recover requires -o/--output "
              "(there would be nowhere to put the capture)",
              file=sys.stderr)
        return 3
    if args.json and do_recover:
        print("note: --json disables recovery "
              "(JSON is not a valid recovery input)",
              file=sys.stderr)
        do_recover = False
        if not args.output:
            print("error: --json alone would discard the capture — "
                  "add -o to write it somewhere", file=sys.stderr)
            return 3

    # ---- capture (in memory) -----------------------------------
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
        ))
    except KeyboardInterrupt:
        return 1

    # ---- optional file output ----------------------------------
    if args.output:
        args.output.write_text(result.text, encoding="utf-8")
        print(f"Wrote {args.output} ({result.message_count} message(s))",
              file=sys.stderr)

    if not do_recover:
        return 0

    # ---- recovery from the in-memory text ----------------------
    return recover_run(RecoverConfig(
        dump_text=result.text,
        out_dir=args.recover_to,
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
