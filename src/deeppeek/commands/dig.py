"""`deeppeek dig` — capture + recover in one step.

Writes the intermediate markdown to disk (--capture-to) and then runs
recovery.  With --apply-to, the recovery is applied directly to an
existing directory tree instead of the standard ./recovered/ layout.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

from ..capture.service import CaptureConfig, capture_to_markdown
from ..recover.pipeline import RecoverConfig, run as recover_run


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("dig", help="Capture a conversation AND recover its files")
    p.add_argument("--capture-to", type=Path, default=None)
    p.add_argument("--recover-to", type=Path, default=None,
                   help="standard layout target (default: ./recovered). "
                        "Mutually exclusive with --apply-to.")
    p.add_argument(
        "--apply-to", type=Path, default=None, metavar="DIR",
        help="apply the recovery directly to an existing directory tree "
             "(recovered files go into DIR, [DEL] files are actually deleted, "
             "reports land beside them).  DIR must exist.",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--watch", action="store_true")
    p.add_argument("--start-prompt")
    p.add_argument("--end-prompt")
    p.add_argument("--match-mode", choices=["substring", "exact", "regex"], default="substring")
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
    p.add_argument("--timeout", type=float, default=None)
    # recovery-side flags
    p.add_argument("--layout", choices=["flat", "header", "passthrough"], default="flat")
    p.add_argument("--min-trust", choices=["HIGH", "MEDIUM", "LOW"], default="MEDIUM")
    p.add_argument("--llm-mode", choices=["off", "smart", "always"], default="off")
    p.add_argument("--llm-url", default=None)
    p.add_argument("--llm-model", default=None)
    p.add_argument("--llm-timeout", type=int, default=None)
    p.add_argument("--trace", action="store_true")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.apply_to is not None and args.recover_to is not None:
        print("error: --apply-to and --recover-to are mutually exclusive",
              file=sys.stderr)
        return 3

    stamp = datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
    capture_path = args.capture_to or Path(f"DeepSeek_{stamp}.md")

    print("[dig 1/2] capturing from browser...", file=sys.stderr)
    try:
        capture_to_markdown(CaptureConfig(
            port=args.port,
            watch=args.watch,
            output=capture_path,
            start_prompt=args.start_prompt,
            end_prompt=args.end_prompt,
            start_index=args.start_index,
            end_index=args.end_index,
            match_mode=args.match_mode,
            context=args.context,
            include_thinking=not args.no_thinking,
            include_hints=not args.no_hints,
            latex=args.latex,
            timeout=args.timeout,
            last_user_turns=args.last,
        ))
    except KeyboardInterrupt:
        return 1

    print(f"[dig 2/2] recovering files from {capture_path}...", file=sys.stderr)

    out_dir = args.recover_to if args.recover_to is not None else Path("./recovered")

    return recover_run(RecoverConfig(
        dump_path=capture_path,
        out_dir=out_dir,
        apply_to=args.apply_to,
        layout=args.layout,
        min_trust=args.min_trust,
        llm_mode=args.llm_mode,
        llm_url=args.llm_url,
        llm_model=args.llm_model,
        llm_timeout=args.llm_timeout,
        verbose=getattr(args, "verbose", False),
        trace=args.trace,
    ))
