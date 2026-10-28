"""`deeppeek prompt snippet` — type a stored prompt by name.

A tiny library of short, reusable prompts that the user can send
into the composer without retyping them.  The snippets live in the
`SNIPPETS` dict in this file.  Add new ones by appending to the
dict; `--list` shows the available names.

    deeppeek prompt snippet implement_and_tests
    deeppeek prompt snippet --list
    deeppeek prompt snippet implement_and_tests --dry-run

No pipe, no Enter, no reply capture, no apply.  This command
exists for exactly one reason: to save the user from retyping
the same instruction across conversations.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from typing import Optional


# ---------------------------------------------------------------------------
# The snippet library.
#
# Keys are the names the CLI accepts; values are the exact prompt
# text that gets typed into the composer.  No templating, no format
# strings — what is written here is what lands in the textarea.
# ---------------------------------------------------------------------------
SNIPPETS: dict[str, str] = {
    "implement_and_tests": (
        "implement the fixes, print every file that you create/edit, "
        "print them fully; dont forget to add tests to help preserve "
        "whatever robustness you implement in future iterations"
    ),
}


def _deeppeek_cmd() -> list[str]:
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _run_cmd(cmd: list[str], *,
             timeout: Optional[float] = None) -> tuple[int, str, str]:
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise SystemExit(f"error: '{cmd[0]}' not on PATH")
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"error: 'deeppeek type' timed out after {timeout}s")
    if proc.returncode != 0:
        print(f"    exit {proc.returncode}", file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _stage_type(text: str, args: argparse.Namespace) -> int:
    fd, path = tempfile.mkstemp(
        prefix="deeppeek_snippet_", suffix=".md",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[type] {len(text):,} chars via 'deeppeek type'",
              file=sys.stderr)
        prefix = _deeppeek_cmd()
        rc, _, _ = _run_cmd(
            prefix + [
                "type", path,
                "--port", str(args.port),
                "--timeout", str(args.timeout),
            ],
            timeout=args.timeout + 5,
        )
        return rc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _format_preview(text: str, indent: str = "    ",
                    width: int = 76, max_lines: int = 4) -> str:
    """Wrap `text` to `width` cols (including `indent`), cap at
    `max_lines`; append an ellipsis if truncated.

    Internal whitespace is normalised first so the wrap is
    predictable regardless of how the snippet was authored.
    `break_long_words=False` keeps identifiers and URLs intact —
    a rare overlong token may push one line past `width`, which is
    preferable to mangling it.
    """
    flat = " ".join(text.split())
    usable = max(20, width - len(indent))
    lines = textwrap.wrap(
        flat, width=usable,
        break_long_words=False, break_on_hyphens=False,
    )
    truncated = len(lines) > max_lines
    lines = lines[:max_lines]
    out = [indent + ln for ln in lines]
    if truncated and out:
        out[-1] = out[-1] + " …"
    return "\n".join(out)


def _print_list() -> None:
    """Print the snippet list to stdout.

    Output is designed to be readable when piped (`--list | less`)
    and to stay compact as the library grows: each snippet gets a
    name line (with char count) and a wrapped, indented preview.
    Prompts longer than `_format_preview`'s cap are truncated with
    ` …`; `--dry-run <name>` always shows the full text.
    """
    if not SNIPPETS:
        print("(no snippets defined)")
        return

    names = sorted(SNIPPETS)
    name_width = max(len(n) for n in names)
    char_width = max(len(str(len(SNIPPETS[n]))) for n in names)

    print(f"Snippets ({len(names)}):")
    print()
    for name in names:
        body = SNIPPETS[name]
        print(f"  {name:<{name_width}}  "
              f"({len(body):>{char_width}} chars)")
        print(_format_preview(body))
        print()
    print("Use: deeppeek prompt snippet <name>")
    print("     (add --dry-run to preview without typing)")


def _report_unknown(name: str) -> int:
    print(f"error: unknown snippet {name!r}", file=sys.stderr)
    print("  available snippets:", file=sys.stderr)
    for avail in sorted(SNIPPETS):
        print(f"    {avail}", file=sys.stderr)
    return 1


def run(args: argparse.Namespace) -> int:
    if args.list:
        _print_list()
        return 0

    if not args.name:
        print("error: missing snippet name (or use --list)",
              file=sys.stderr)
        for avail in sorted(SNIPPETS):
            print(f"    {avail}", file=sys.stderr)
        return 1

    if args.name not in SNIPPETS:
        return _report_unknown(args.name)

    text = SNIPPETS[args.name]
    if not text.strip():
        print(f"error: snippet {args.name!r} is empty",
              file=sys.stderr)
        return 1

    print(f"[build] snippet {args.name!r}: {len(text):,} chars",
          file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] snippet below; not typed.", file=sys.stderr)
        print(text)
        return 0

    return _stage_type(text, args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "snippet",
        help="Type a stored snippet into the composer by name",
    )
    p.add_argument(
        "name", nargs="?", default=None,
        help="snippet name (see --list for available names)",
    )
    p.add_argument(
        "--list", action="store_true",
        help="list available snippet names and exit",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="port for the local extension server (default: 8765)",
    )
    p.add_argument(
        "--timeout", type=float, default=60.0, metavar="SECONDS",
        help="how long to wait for the extension to connect and ack "
             "(default: 60)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="print the snippet to stdout and stop (no type)",
    )
    p.set_defaults(func=run)
