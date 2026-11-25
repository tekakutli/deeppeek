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

File input
----------
--file PATH (or -f PATH) reads the given file as the input text; '-' reads
stdin.  When positional args are also present, they are prepended as an
instruction, separated from the file contents by a blank line:

    deeppeek answer --file notes.txt
    deeppeek answer "Summarize the following:" --file notes.txt
    cat notes.txt | deeppeek answer --file -
    deeppeek answer --script passthrough --file notes.txt

Tab hints
---------
When the URL points at a deeppeek daemon, the flags below travel to
the daemon in the request body under `deeppeek.tab` and let you pick
which browser tab the request runs in:

  · --new-tab                 (default) open a new tab
  · --container NAME          open a new tab in the named Firefox
                              container
  · --container-id ID         open a new tab in the raw cookieStoreId
  · --reuse-tab               reuse whichever tab is currently active
  · --reuse-tab-in NAME       reuse an existing tab in container NAME,
                              or create one if none exists
  · --reuse-tab-by TAB_ID     pin to an already-known tab id
                              (get one from `deeppeek daemon --which-tab`)

Tab hints only take effect when `--url` points directly at the
daemon (e.g. http://localhost:8766/v1/chat/completions).  An
intermediary like llama-swap must preserve the `deeppeek` body field
for hints to reach the daemon.

The flag definitions and validation live in `_tab_hints.py` so they
stay in lockstep with `deeppeek editor`.
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
from . import _tab_hints


def register(sub):
    p = sub.add_parser(
        "answer",
        help="Send a one-shot question to the local OpenAI-compatible LLM",
    )
    p.add_argument(
        "args", nargs="*", metavar="ARG",
        help="either a literal prompt, or <script-name> <input-text> "
             "(optional when --file is given)",
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
        "-f", "--file", default=None, metavar="PATH",
        help="Read input text from PATH ('-' = stdin). If positional args "
             "are also given, they are prepended as an instruction.",
    )
    p.add_argument(
        "--list", action="store_true",
        help="List registered scripts and exit",
    )

    # ---- streaming toggle -----------------------------------------
    p.add_argument(
        "--stream", dest="stream", action="store_true", default=True,
        help="Stream the reply as SSE deltas arrive (default).  "
             "Deltas print to stderr as they arrive.",
    )
    p.add_argument(
        "--no-stream", dest="stream", action="store_false",
        help="Wait for the full reply before printing (no SSE).  "
             "Use this if streaming seems to hang or produce "
             "nothing — it isolates client-side SSE parsing from "
             "server-side streaming.",
    )

    # ---- tab hints (shared with `deeppeek editor`) ----------------
    _tab_hints.register_tab_flags(p)

    p.set_defaults(func=run)
    return p


def _read_path(path: str) -> Optional[str]:
    """Return the contents of `path` (or stdin for '-'). None on error."""
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read {path!r}: {e}", file=sys.stderr)
        return None


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

    file_text: Optional[str] = None
    if args.file is not None:
        file_text = _read_path(args.file)
        if file_text is None:
            return 1

    script_name: Optional[str] = args.script
    positional_text = " ".join(args.args)

    if not script_name and not args.literal and args.args:
        candidate = args.args[0]
        if llm_scripts.is_script(candidate):
            script_name = candidate
            positional_text = " ".join(args.args[1:])

    if script_name and not llm_scripts.is_script(script_name):
        print(
            f"error: unknown script {script_name!r} — try "
            f"`deeppeek answer --list`",
            file=sys.stderr)
        return 1

    if file_text is not None:
        if positional_text:
            input_text = positional_text + "\n\n" + file_text
        else:
            input_text = file_text
    else:
        if not positional_text and not script_name:
            print(
                "error: no input — provide a prompt or use --file",
                file=sys.stderr)
            return 1
        input_text = positional_text

    # Validate tab flags before doing any network work, so bad
    # combinations fail fast.
    try:
        tab_hints = _tab_hints.build_tab_hints(args)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1

    client = LLMClient(
        url=args.url,
        model=args.model,
        timeout=args.timeout,
        cache_dir=args.cache_dir,
        stream=args.stream,
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

    deeppeek_hints = {"tab": tab_hints} if tab_hints else None

    out = client.call(
        prompt,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        tag=tag,
        deeppeek_hints=deeppeek_hints,
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
