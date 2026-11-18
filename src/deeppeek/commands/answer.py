"""deeppeek answer <question> — one-shot question to the local LLM.

Thin CLI wrapper around llm.LLMClient.  No streaming, no conversation
state — send a prompt, print the reply, exit.  For multi-turn work use
the daemon/hijack path instead.
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


def register(sub):
    p = sub.add_parser(
        "answer",
        help="Send a one-shot question to the local OpenAI-compatible LLM",
    )
    p.add_argument("question", nargs="+", help="Question / prompt to send")
    p.add_argument("--url", default=DEFAULT_LLM_URL)
    p.add_argument("--model", default=DEFAULT_LLM_MODEL)
    p.add_argument("--timeout", type=int, default=DEFAULT_LLM_TIMEOUT)
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="If set, cache replies by (model, prompt) hash",
    )
    p.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip the health probe (saves ~1 round-trip on slow endpoints)",
    )
    p.set_defaults(func=run)
    return p


def run(args) -> int:
    prompt = " ".join(args.question)

    client = LLMClient(
        url=args.url,
        model=args.model,
        timeout=args.timeout,
        cache_dir=args.cache_dir,
    )

    if not args.no_probe and not client.probe(DEFAULT_PROBE_TIMEOUT):
        print(f"error: LLM endpoint unreachable at {args.url}", file=sys.stderr)
        return 1

    out: Optional[str] = client.call(
        prompt,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        tag="answer",
    )
    if out is None:
        return 1

    print(out)
    return 0
