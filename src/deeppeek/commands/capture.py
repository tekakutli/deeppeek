"""deeppeek capture — capture a DeepSeek conversation.

Modes:

  capture                    recover in memory → ./deeppeek_recovered/
  capture -o chat.md         write chat.md AND recover; the recovered
                             dir is created next to chat.md
  capture -o auto            auto-named chat file + recover
  capture --apply-to DIR     recover in memory → applied directly to DIR
  capture --apply-here       recover in memory → applied directly to cwd
  capture --json -o chat.md  write JSON, no recovery
  capture -o chat.md \
      --no-recover           write chat.md only, no recovery

Recovery is the default.  -o only adds an on-disk write of the
markdown; it does not suppress recovery.  --no-recover is the
opt-out, and it requires -o so the capture isn't discarded.

When a file is written (via -o), recovery reads it back from disk so
that manifest.dump.path records the real path rather than
<in-memory>.  When -o is absent, recovery runs on the in-memory
text and the manifest records <in-memory>.

The recovered directory defaults to ./deeppeek_recovered/.  When
-o PATH is given, it is created next to PATH instead — so
-o /tmp/conv.md writes /tmp/deeppeek_recovered/, not into the
current directory.

Output layout is derived, not user-set:

    --apply-to / --apply-here set   → header (target tree preserved)
    neither                          → flat   (basenames only)

There is no user-facing layout flag and no --remap.
"""
import argparse
import sys
from pathlib import Path
from typing import Optional

from ..capture.service import (
    CaptureConfig,
    capture_conversation,
    default_output_path,
)
from ..recover.pipeline import RecoverConfig, run as recover_run


# ---------------------------------------------------------------------------
# Shared capture-side args (used by capture AND conversation)
# ---------------------------------------------------------------------------

def add_capture_side_args(p: argparse.ArgumentParser) -> None:
    """Add the capture-side arguments shared by capture and conversation.

    -o is NOT included: capture needs to accept the magic string
    auto, so it defines -o itself.  conversation uses a plain
    Path default.  Everything else (slicer, LLM-free capture options)
    is identical between the two commands.
    """
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
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--no-hints", action="store_true")
    p.add_argument("--latex", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of Markdown")
    p.add_argument("--timeout", type=float, default=None)


def capture_config_from_args(args: argparse.Namespace) -> CaptureConfig:
    """Build a CaptureConfig from the shared capture-side args.

    output is always None: both callers write the returned text to
    disk themselves (capture to decide auto vs path, conversation to
    apply its conversation.md default).  capture_conversation does
    not write, so this field is unused on this code path.
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
    )


# ---------------------------------------------------------------------------
# Register / run
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "capture",
        help="Capture a DeepSeek conversation and recover its files",
    )
    p.add_argument(
        "-o", "--output", type=str, default=None,
        metavar="PATH|auto",
        help="also write the markdown to PATH (or 'auto' for a "
             "timestamped default like DeepSeek_2026-10-21_15-42-03.md). "
             "When PATH is given, the recovered directory is created "
             "next to it (e.g. -o /tmp/conv.md → /tmp/deeppeek_recovered/). "
             "Recovery runs by default; use --no-recover to skip it.",
    )
    p.add_argument(
        "--recover", action="store_true",
        help="(no-op; recovery is now the default.  Kept for backward "
             "compatibility — use --no-recover to opt out.)",
    )
    p.add_argument(
        "--no-recover", action="store_true",
        help="skip recovery; write the markdown file only (requires -o; "
             "nothing would be saved otherwise)",
    )
    add_capture_side_args(p)

    # ---- recovery-side flags ----
    p.add_argument("--recover-to", type=Path, default=None)
    p.add_argument("--apply-to", type=Path, default=None, metavar="DIR")
    p.add_argument(
        "--apply-here", action="store_true",
        help="shorthand for --apply-to with the current working directory "
             "(resolved to an absolute path).  Mutually exclusive with "
             "--apply-to and --recover-to.",
    )
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


def run(args: argparse.Namespace) -> int:
    # ---- decide recovery intent ----
    if args.apply_here and args.apply_to is not None:
        print("error: --apply-here and --apply-to are mutually exclusive",
              file=sys.stderr)
        return 3

    apply_to = Path.cwd().resolve() if args.apply_here else args.apply_to

    if apply_to is not None and args.recover_to is not None:
        print("error: --apply-to/--apply-here and --recover-to are "
              "mutually exclusive", file=sys.stderr)
        return 3
    if args.no_recover and apply_to is not None:
        print("error: --no-recover and --apply-to/--apply-here are mutually "
              "exclusive (--apply-to/--apply-here implies recovery)",
              file=sys.stderr)
        return 3

    # Recovery is on by default — with or without -o, with or without
    # --last, with or without --apply-to.  --no-recover is the only
    # opt-out (and it requires -o so the capture isn't discarded).
    # --recover is kept as an accepted-but-redundant flag for scripts
    # written against the old behaviour.
    do_recover = not args.no_recover

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

    try:
        result = capture_conversation(capture_config_from_args(args))
    except KeyboardInterrupt:
        return 1

    resolved_output: Optional[Path] = None
    if args.output is not None:
        if args.output == "auto":
            resolved_output = default_output_path(result.title, args.json)
        else:
            resolved_output = Path(args.output)
        resolved_output.write_text(result.text, encoding="utf-8")
        print(f"Wrote {resolved_output} ({result.message_count} message(s))",
              file=sys.stderr)

    if not do_recover:
        return 0

    if args.recover_to is not None:
        out_dir = args.recover_to
    elif resolved_output is not None:
        # Put the recovered directory next to the -o output, so
        # -o /tmp/conv.md writes to /tmp/deeppeek_recovered/
        # rather than littering the cwd.  -o auto and a bare
        # filename both resolve to Path("."), so they land in the
        # cwd — matching the no--o default below.
        out_dir = resolved_output.parent / "deeppeek_recovered"
    else:
        out_dir = Path("./deeppeek_recovered")

    if resolved_output is not None:
        dump_path: Optional[Path] = resolved_output
        dump_text: Optional[str] = None
    else:
        dump_path = None
        dump_text = result.text

    return recover_run(RecoverConfig(
        dump_path=dump_path,
        dump_text=dump_text,
        out_dir=out_dir,
        apply_to=apply_to,
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
