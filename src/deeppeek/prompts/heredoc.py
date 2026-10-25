"""`deeppeek prompt heredoc` — install a standing heredoc-emission order.

Types a single, focused instruction into the DeepSeek composer asking
the AI to emit every future file — code, markdown, configs, tests,
documentation — as a quoted bash heredoc piped through python3,
rather than as raw markdown.

This is the standalone, forward-looking counterpart to the
HARD_CONSTRAINT block that `evolve.py` and `reprint_markdown.py`
embed in their own one-shot prompts:

  · `evolve.py` — the constraint applies to the task it defines.
  · `reprint_markdown.py` — the constraint applies to a reprint pass.
  · `heredoc.py` (this module) — the constraint is a STANDING ORDER,
    applying to every file the AI emits for the rest of the
    conversation.

Three properties by design:

  · It is ONE-SHOT, IDEMPOTENT.  Sending it once re-anchors the AI;
    sending it twice costs one message and does no harm.

  · It is RECOVERY-AWARE.  The instruction explicitly tells the AI
    to re-emit files that arrived broken, so a conversation already
    half-mangled can be healed in place rather than restarted.

  · It is NOT a task prompt.  There is no file to pipe, no apply
    step, no reprint — just the ordering.

The prompt text is backtick-free by construction: it contains
examples the AI is meant to copy, and a raw backtick in an example
would teach the AI to emit raw backticks.  The same discipline
applies to the surrounding module — error messages and progress
lines use single quotes, never backticks.

For the rationale behind each rule (why quoted heredoc, why
chr(96), why the triple-quote trap), see docs/EMISSION.md.  That
document is for humans; it is not piped.  If you want to send it
anyway, --text "$(cat docs/EMISSION.md)" works, but it dilutes the
AI's attention and is not the recommended path.

Pipeline:

    build    use HEREDOC_PROMPT (or --text override)
    type     `deeppeek type <tempfile>`       — the instruction text

No pipe, no Enter, no reply capture, no apply.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Optional


HEREDOC_PROMPT = """\
STANDING ORDER — HOW TO EMIT FILES IN THIS CONVERSATION

This conversation is happening through a GUI that renders markdown
imperfectly. In particular, it mangles nested code fences. Any file
you emit whose content itself contains a three-backtick fence marker
will come back to me as broken text, and I will not be able to
recover the file from the transcript.

From now on — for every file you write, edit, create, or reprint in
this conversation, including code, markdown, configs, tests, and
documentation — you MUST use the bash-heredoc pattern below. Do not
emit file content as raw markdown. Do not wrap bash in a markdown
code fence. Do not assume a previous emission was "close enough."

Canonical pattern for a single file:

    python3 - <<'PYEOF'
    from pathlib import Path
    B = chr(96) * 3   # three backticks; chr(96) is the backtick
    content = "\\n".join([
        "# heading",
        "",
        B + "python",
        "x = 1",
        B,
        "",
    ])
    Path("path/to/file.md").write_text(content)
    print("wrote path/to/file.md")
    PYEOF

Canonical pattern for multiple files in one block:

    python3 - <<'PYEOF'
    from pathlib import Path
    B = chr(96) * 3

    def w(path, lines):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("\\n".join(lines) + "\\n")
        print("wrote", path)

    w("a/b.py", [
        "def f():",
        "    return 1",
    ])
    w("a/c.md", [
        "# heading",
        B + "python",
        "x = 1",
        B,
    ])
    PYEOF

Rules for the bash itself:

  · Use a QUOTED heredoc (<<'PYEOF'), so bash does not expand
    dollar signs, backticks, or quotes inside the body. This is what
    makes the body safe to carry arbitrary content.

  · Never put a literal backtick character in the bash you emit.
    Use chr(96) — or chr(96)*3 — to construct backticks at Python
    runtime. A literal backtick in the bash text will be mangled by
    the GUI before it reaches me.

  · Never wrap the bash itself in a markdown code fence. Present it
    as plain text.

  · Multiple files per heredoc block is preferred: it keeps the
    response short and means fewer chances for the GUI to mangle
    something.

  · Each file write prints "wrote <path>", so the block's success is
    verifiable from its own output alone.

This is a standing order. It applies to every file you emit in this
conversation from this point forward, not only the next one. If you
have already emitted a file that arrived broken because it contained
raw markdown fences or literal backticks, re-emit it now using the
pattern above.
"""


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
        prefix="deeppeek_heredoc_", suffix=".md",
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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    prompt = args.text if args.text is not None else HEREDOC_PROMPT

    if not prompt.strip():
        print("error: prompt text is empty/whitespace-only",
              file=sys.stderr)
        return 1

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
        "heredoc",
        help="Type a standing instruction asking the AI to emit every "
             "future file via a python heredoc",
    )

    p.add_argument(
        "--text", default=None,
        help="override the built-in prompt text.  Defaults to "
             "HEREDOC_PROMPT.  Useful for testing or for customising "
             "the constraint.",
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
        help="print the prompt to stdout and stop (no type)",
    )
    p.set_defaults(func=run)
