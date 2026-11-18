"""deeppeek answer <question> — one-shot question to the local LLM.

Three modes:

  · Literal (default):
        deeppeek answer "hi"

  · Script:
        deeppeek answer passthrough "text"
        deeppeek answer --script passthrough "text"

    First positional arg (or --script) names a registered script;
    remaining args become the script's input text.  The script's
    build_prompt wraps input → prompt; its optional on_response
    hook post-processes the LLM reply.

  · List scripts:
        deeppeek answer --list

  · Force literal even when the first arg matches a script name:
        deeppeek answer --literal "passthrough"

See llm_scripts/__init__.py for the script contract.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from ..llm import (
    LLMClient,
    DEFAULT_LLM_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_PROBE_TIMEOUT,
)
from .. import llm_scripts


def register(sub):
    p = sub.add_parser(
        "answer",
        help="Send a one-shot question to the local OpenAI-compatible LLM",
    )
    p.add_argument(
        "args", nargs="+", metavar="ARG",
        help="either a literal prompt, or <script-name> <input-text>",
    )
    p.add_argument("--url", default=DEFAULT_LLM_URL)
    p.add_argument("--model", default=DEFAULT_LLM_MODEL)
    p.add_argument("--timeout", type=int, default=DEFAULT_LLM_TIMEOUT)
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument(
        "--cache-dir", type=Path, default=None,
        help="If set, cache replies by (model, prompt) hash",
    )
    p.add_argument(
        "--no-probe", action="store_true",
        help="Skip the health probe",
    )
    p.add_argument(
        "--script", default=None, metavar="NAME",
        help="Use a registered script explicitly (overrides positional "
             "detection)",
    )
    p.add_argument(
        "--literal", action="store_true",
        help="Force literal-prompt mode even if first arg matches a "
             "script name",
    )
    p.add_argument(
        "--list", action="store_true",
        help="List registered scripts and exit",
    )
    p.set_defaults(func=run)
    return p


def run(args) -> int:
    if args.list:
        reg = llm_scripts.list_scripts()
        if not reg:
            print("(no scripts registered)", file=sys.stderr)
            return 0
        for name in sorted(reg):
            desc = getattr(reg[name], "DESCRIPTION", "")
            print(f"{name}\t{desc}")
        return 0

    script_name: Optional[str] = args.script
    input_text = " ".join(args.args)

    if not script_name and not args.literal and args.args:
        candidate = args.args[0]
        if llm_scripts.is_script(candidate):
            script_name = candidate
            input_text = " ".join(args.args[1:])

    if script_name and not llm_scripts.is_script(script_name):
        print(
            f"error: unknown script {script_name!r} — try "
            f"`deeppeek answer --list`",
            file=sys.stderr)
        return 1

    client = LLMClient(
        url=args.url,
        model=args.model,
        timeout=args.timeout,
        cache_dir=args.cache_dir,
    )

    if not args.no_probe and not client.probe(DEFAULT_PROBE_TIMEOUT):
        print(
            f"error: LLM endpoint unreachable at {args.url}",
            file=sys.stderr)
        return 1

    if script_name:
        mod = llm_scripts.load_script(script_name)
        ctx = llm_scripts.build_ctx(
            script=script_name, model=args.model)
        prompt = mod.build_prompt(input_text, ctx)
        tag = f"answer:{script_name}"
    else:
        mod = None
        ctx = None
        prompt = input_text
        tag = "answer"

    out = client.call(
        prompt,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        tag=tag,
    )
    if out is None:
        return 1

    if mod is not None and callable(getattr(mod, "on_response", None)):
        try:
            display = mod.on_response(out, ctx)
        except Exception as e:
            print(
                f"  [script:{script_name}] on_response threw: {e}",
                file=sys.stderr)
            display = out
        if display is None:
            display = out
    else:
        display = out

    print(display)
    return 0