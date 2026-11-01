"""`deeppeek prompt split` — type a file-splitting prompt into the composer.

Types (does not submit) a fixed prompt asking DeepSeek to split a
file into more-stable and more-dynamic subfiles, without degrading
performance.  The named file defaults to the literal word `file`
when no path is supplied.

Pair with `deeppeek enter` to submit.
"""
import argparse
import sys

from ..commands._composer import submit_request


_PROMPT_TEMPLATE = (
    "This is my {file}. As you can see, it has grown large. "
    "If it is possible to split it without impairing its features or "
    "degrading its performance, I want you to split it into subfiles. "
    "If it is possible, then I want you to try to make it so that the "
    "subfiles are divided into more stable blocks that are unlikely to "
    "change in the future, and more dynamic ones that are likely to be "
    "reworked or expanded upon."
)


def _build_prompt(file_arg: str | None) -> str:
    return _PROMPT_TEMPLATE.format(file=file_arg if file_arg else "file")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "split",
        help="Type a file-splitting prompt into the DeepSeek composer "
             "(does not submit)",
    )
    p.add_argument(
        "file", nargs="?", default=None, metavar="FILE",
        help="relative path of the file to name in the prompt "
             "(default: the literal word 'file')",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="how long to wait for the extension to connect and ack "
             "(default: 30)",
    )
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    text = _build_prompt(args.file)

    print(f"Typing split prompt ({len(text):,} chars)...",
          file=sys.stderr)

    rc, result = submit_request(
        "type", {"text": text},
        timeout=args.timeout, ack_name="type",
        port=args.port,
    )

    if rc == 0:
        sel = result.get("composer_selector") or "(unknown)"
        source = result.get("composer_source") or "?"
        print(f"OK — split prompt in composer via {sel} "
              f"(source={source})", file=sys.stderr)
        return 0
    if rc == 2:
        print(f"Split failed: {result.get('reason') or '(no reason)'}",
              file=sys.stderr)
        for k in ("composer_selector", "composer_source",
                  "react_accepted",
                  "composer_element_info", "active_element_info"):
            if k in result:
                print(f"  {k}: {result[k]}", file=sys.stderr)
        return 2
    return rc
