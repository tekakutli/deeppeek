"""`deeppeek prompt` — AI prompt pipelines.

This is a namespace.  Each prompt gets its own subcommand:

    deeppeek prompt evolve <flags>   — evolve deeppeek via a conversation
    deeppeek prompt <future> ...     — other prompts, added later

Every prompt module under `deeppeek.prompts.*` provides:

    register(subparsers)          — add its subcommand to the `prompt` group
    run(args) -> int              — orchestrate the pipeline

`prompt.run` is only a trampoline: it hands off to whichever
subcommand argparse dispatched to.
"""
import argparse


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "prompt",
        help="AI prompt pipelines (elicit, await, apply)",
    )
    psub = p.add_subparsers(dest="prompt_cmd", required=True)

    from ..prompts import evolve
    evolve.register(psub)

    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return args.func(args)
