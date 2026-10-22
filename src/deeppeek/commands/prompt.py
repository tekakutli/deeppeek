"""`deeppeek prompt` — AI prompt pipelines.

Each prompt gets its own subcommand:

    deeppeek prompt evolve <flags>   — evolve deeppeek via a conversation
    deeppeek prompt new [flags]      — seed a prompt about the current dir

Every prompt module under `deeppeek.prompts.*` provides:

    register(subparsers)          — add its subcommand
    run(args) -> int              — orchestrate the pipeline
"""
import argparse


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "prompt",
        help="AI prompt pipelines (elicit, await, apply)",
    )
    psub = p.add_subparsers(dest="prompt_cmd", required=True)

    from ..prompts import evolve, new
    evolve.register(psub)
    new.register(psub)

    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return args.func(args)
