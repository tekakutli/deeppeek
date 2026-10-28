"""`deeppeek prompt reprint` — ask the AI to reprint the files that
are still in the residual.

The setup: an AI conversation produced edits to one or more files.
Applying them through `recover --apply-here` landed *most* of them,
but a handful stayed in the residual — their blocks didn't reach a
successful placement, so the target tree still holds the pre-edit
version.  This prompt tells the AI, in the same conversation, to
re-emit exactly those files, in full, using the bash-heredoc
emission pattern that survives the GUI.

Two properties by design:

  · SURGICAL.  Only the files listed in the residual are reprinted.
    Files that already landed are named nowhere in the prompt, so
    the AI has no reason to touch them.  This is the difference
    from `prompt reprint-markdown`, which blindly reprints every
    markdown the AI edited — sometimes overwriting already-correct
    content with an older snapshot.

  · RECOVERY-AWARE.  The prompt names the specific failure ("some
    blocks did not land"), names the files, and asks for full
    reprints.  The AI knows *why* it's being asked to re-emit, so
    it doesn't reply with an inline "here are the changed lines"
    fragment — which is what caused the failure in the first place.

The file list is read from a residual.md on disk — the same file
`deeppeek residual` inspects.  Default path: ./deeppeek/residual.md,
which is where `recover --apply-here` writes when run from the
target tree's root.

Pipeline:

    parse    read residual.md, extract unique file list
    build    format the prompt with the file list
    type     `deeppeek type <tempfile>`       — the prompt

No pipe, no Enter, no reply capture, no apply.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from ..recover.residual import (
    parse_residual_entries,
    unique_residual_paths,
)


DEFAULT_RESIDUAL = Path("deeppeek/residual.md")


# REPRINT_PROMPT = """\

# {file_list}

# Please reprint EVERY file in that list, in full, at its current
# (final) state.

# """
REPRINT_PROMPT = """\
{file_list}

Please reprint EVERY file in that list, in full, at its current
(final) state. For long files, the whole function or block
containing each edit is also acceptable — but never just the
changed lines.

"""


# ---------------------------------------------------------------------------
# Subprocess helpers — same shape as the other prompt modules.
# ---------------------------------------------------------------------------

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
        raise SystemExit(f"error: `{cmd[0]}` not on PATH")
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"error: timed out after {timeout}s")
    if proc.returncode != 0:
        print(f"    exit {proc.returncode}", file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _format_file_list(paths: list[str]) -> str:
    return "\n".join(f"  - {p}" for p in paths)


def _build_prompt(paths: list[str]) -> str:
    return REPRINT_PROMPT.format(file_list=_format_file_list(paths))


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _load_residual_paths(path: Path) -> tuple[Optional[list], Optional[str]]:
    """Return `(paths, error)`.  Exactly one of the two is None."""
    if not path.is_file():
        return None, (
            f"no residual file at {path}\n"
            f"  (default: ./deeppeek/residual.md — write this with "
            f"`deeppeek recover <conversation.md> --apply-here`)")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"cannot read {path}: {e}"

    entries = parse_residual_entries(text)
    if not entries:
        return None, f"{path} contains no residual blocks"
    paths = unique_residual_paths(text)
    if not paths:
        return None, (
            f"{path} contains {len(entries)} block(s) but none have "
            f"a `## File:` header — there is nothing to reprint")
    return paths, None


def _stage_type(prompt: str, args: argparse.Namespace) -> int:
    fd, path = tempfile.mkstemp(
        prefix="deeppeek_reprint_", suffix=".md",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        print(f"[type] {len(prompt):,} chars via `deeppeek type`",
              file=sys.stderr)
        prefix = _deeppeek_cmd()
        rc, _, _ = _run_cmd(
            prefix + ["type", path, "--port", str(args.port)],
            timeout=args.type_timeout,
        )
        return rc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    paths, err = _load_residual_paths(args.residual)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 1

    print(f"[residual] {args.residual}: "
          f"{len(paths)} unique file(s)", file=sys.stderr)
    for p in paths:
        print(f"  {p}", file=sys.stderr)

    prompt = _build_prompt(paths)
    print(f"[build] prompt: {len(prompt):,} chars", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] prompt below; not typed.", file=sys.stderr)
        print(prompt)
        return 0

    return _stage_type(prompt, args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "reprint",
        help="Ask the AI to reprint every file still in the residual, "
             "in full",
    )
    p.add_argument(
        "--residual", type=Path, default=DEFAULT_RESIDUAL, metavar="PATH",
        help=f"path to residual.md (default: {DEFAULT_RESIDUAL})",
    )
    p.add_argument(
        "--type-timeout", type=float, default=600.0,
        help="seconds for `deeppeek type` (default: 600)",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--dry-run", action="store_true",
        help="print the prompt to stdout and stop (no type)",
    )
    p.set_defaults(func=run)
