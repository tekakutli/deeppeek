"""`deeppeek prompt` — AI prompt pipelines.

Each prompt gets its own subcommand:

    deeppeek prompt evolve <flags>             — evolve deeppeek via a conversation
    deeppeek prompt new [flags]                — seed a prompt about the current dir
    deeppeek prompt last-thought [flags]       — extract the last thought process
    deeppeek prompt heredoc [flags]            — install a standing heredoc-emission order
    deeppeek prompt reprint-markdown [flags]   — ask the AI to reprint all edited markdowns
    deeppeek prompt review [flags]             — reset, re-apply the conversation, and pipe the aftermath
    deeppeek prompt refine_deeppeek [flags]    — like review, plus a dump of deeppeek itself
    deeppeek prompt delete [flags]             — remove all attached files and clear the composer textarea
    deeppeek prompt attachments [flags]        — download files attached to messages
                                                 (composer draft/edit, prompt regex, message id,
                                                 last N, or all)
    deeppeek prompt retry [flags]              — reset the composer with the last
                                                 prompt-with-attachments (clears, re-attaches,
                                                 retypes the text)

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

    from ..prompts import (
        evolve, new, last_thought, heredoc, reprint_markdown, review,
        refine_deeppeek, delete, attachments, retry,
        reprint,
        snippet,
    )
    evolve.register(psub)
    new.register(psub)
    last_thought.register(psub)
    heredoc.register(psub)
    reprint_markdown.register(psub)
    review.register(psub)
    refine_deeppeek.register(psub)
    delete.register(psub)
    attachments.register(psub)
    retry.register(psub)
    reprint.register(psub)
    snippet.register(psub)

    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return args.func(args)
