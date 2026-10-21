"""`deeppeek recover` — thin adapter over recover.pipeline.run()."""
import argparse
from pathlib import Path

from ..recover.pipeline import RecoverConfig, run as recover_run


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("recover", help="Recover source files from a markdown dump")
    p.add_argument("dump", type=Path)
    p.add_argument("--out", type=Path, default=Path("./recovered"))
    p.add_argument("--layout", choices=["flat", "header", "passthrough"], default="flat")
    p.add_argument("--remap", action="append", default=[], metavar="OLD=NEW")
    p.add_argument("--min-trust", choices=["HIGH", "MEDIUM", "LOW"], default="MEDIUM")
    p.add_argument("--llm-mode", choices=["off", "smart", "always"], default="off")
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
    cfg = RecoverConfig(
        dump_path=args.dump,
        out_dir=args.out,
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
    )
    return recover_run(cfg)
