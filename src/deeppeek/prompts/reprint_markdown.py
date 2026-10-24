"""`deeppeek prompt reprint-markdown` — ask the AI to reprint all edited markdowns.

The setup this prompt addresses: an AI conversation has produced
one or more markdown edits (READMEs, docs, changelogs, configs).
Some of those edits may have reached the target tree and some may
have been lost — typically because the markdown was emitted inside
an outer fenced block and the parser mangled nested code fences.

The recovery path that deeppeek ships (`--apply-to`) can only patch
against a base that already exists on disk.  It cannot create a
markdown that was never fully emitted.  This prompt closes that gap
by asking the AI, in the same conversation, to reprint every
markdown it has written or edited, at its current state, using the
bash-heredoc pattern that survives the GUI.

Three properties by design:

  · It is CONVERSATIONAL, not file-system aware.  It does not scan
    the target tree, does not pipe any dumps, and does not know
    which markdowns exist.  The AI knows what it edited; the user
    knows what to do with the output.

  · It is NARROW.  Only markdown files (`*.md`).  Not code, not
    configs.  Code patches go through `deeppeek recover` against a
    base that exists; the whole point of this prompt is the
    markdown case where the base might not exist.

  · It is IDEMPOTENT.  Running it twice produces the same bash.  If
    the AI has already printed a markdown correctly, the reprint is
    byte-identical to what's on disk — `Path.write_text` is a no-op
    diff.

The pipeline is two stages, mirroring `evolve.py` but with no
generate stage:

    build    format the prompt
    type     `deeppeek type <tempfile>`  ← the prompt text

No pipe, no Enter, no reply capture, no apply.  The user reviews
what the AI produces, then copies the bash block into a terminal.

Usage:

    deeppeek prompt reprint-markdown           # type the prompt
    deeppeek prompt reprint-markdown --dry-run # print only

Note on HARD_CONSTRAINT duplication: this module carries its own
copy, rather than importing it from `evolve.py`.  Two prompts share
it today; extraction into `_common.py` is a one-evening refactor if
a third prompt needs it.  Until then, edits to the constraint
wording must be applied in both places.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Optional


# ---------------------------------------------------------------------------
# HARD_CONSTRAINT — copied verbatim from prompts/evolve.py
# ---------------------------------------------------------------------------
#
# This is the mandatory bash-heredoc emission rule.  Every prompt that
# asks the AI to emit file contents carries a copy.  See the module
# docstring for why this module does not import it from evolve.py.

HARD_CONSTRAINT = """\
HARD CONSTRAINT — HOW TO EMIT FILES

This conversation is happening through a GUI that renders markdown
imperfectly. In particular, it mangles nested code fences. Any file
you emit whose content itself contains a three-backtick fence marker
will come back to me as broken text, and I will not be able to
recover the file from the transcript. A Python file with a docstring
that illustrates markdown, a test fixture that itself contains
fenced code, a README that documents code — all of these are
affected.

The rule: NEVER emit file contents as raw markdown. NEVER wrap a
bash command in a markdown code fence. ALWAYS emit files via a bash
heredoc piped through python3, and construct any backtick characters
with chr(96).

Canonical pattern for a single file:

    python3 - <<'PYEOF'
    from pathlib import Path
    B = chr(96) * 3   # three backticks; chr(96) is the backtick
    content = "\\n".join([
        "# heading",
        "",
        B + "js",
        "const x = 1;",
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
        "'''docstring'''",
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

  · Use a QUOTED heredoc (`<<'PYEOF'`), so bash does not expand
    anything inside.  This is what makes dollar signs, backticks,
    and quotes safe.
  · Do not put a literal backtick character in the bash you emit.
    Use chr(96) — or chr(96)*3 — to build backticks at Python
    runtime.
  · Do not wrap the bash itself in a markdown code fence.  Present
    it as plain text.
  · Multiple files per heredoc block is preferred: it keeps the
    response short and means fewer chances for the GUI to mangle
    something.
  · Each file write prints "wrote <path>", so you can verify the
    whole block succeeded from its output alone.

This constraint is not stylistic.  It is the only way your fixes
will reach my file system.  Ignore it and I cannot run what you
produce.
"""


REPRINT_PROMPT = """\
In this conversation, you and I have edited or created one or more
markdown files — READMEs, documentation, changelogs, configs.  Some
of those edits may have been lost: either the file was never
written to disk, or the markdown was emitted inside an outer code
fence whose nested inner fences broke the output and fragmented it
beyond recovery.

Please reprint EVERY markdown file you have written or edited in
this conversation, at its current (final) state, fully, using the
bash-heredoc pattern described below.  Include files even if you
believe you already printed them correctly — the purpose of this
exercise is to ensure each one is fully emitted exactly once, in a
form that survives the transport.  If a markdown file was edited
multiple times, print only its most recent state.  Skip any
markdown file that was discussed or planned but never fully
written.

Rules for the reprint:

  · One bash block per markdown file.  Each block writes exactly
    one file and prints "wrote <path>".
  · Use the same paths the original emission used.  Do not
    reinterpret the paths; if a previous bash block wrote
    "docs/README.md", this one writes "docs/README.md".
  · Output only the bash.  No prose before, between, or after the
    blocks.  If you want to identify which block corresponds to
    which file, use a single comment line at the top of each bash
    block.
  · Only markdown files (*.md).  No .py, no .js, no .json, no
    .toml, no .yaml — those go through a different recovery path
    and are not this prompt's concern.
  · If the conversation has not edited or written any markdown
    files, reply with exactly this single line and nothing else:
    "no markdown files were written in this conversation"

---

{hard_constraint}
"""


def _deeppeek_cmd() -> list[str]:
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _run_cmd(cmd: list[str], *,
             timeout: Optional[float] = None,
             allow_failure: bool = False) -> tuple[int, str, str]:
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        msg = f"`{cmd[0]}` not on PATH"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    except subprocess.TimeoutExpired:
        msg = f"timed out after {timeout}s"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    if proc.returncode != 0:
        tag = "(allowed)" if allow_failure else ""
        print(f"    exit {proc.returncode} {tag}".rstrip(), file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _build_prompt() -> str:
    return REPRINT_PROMPT.format(hard_constraint=HARD_CONSTRAINT)


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
            allow_failure=False,
        )
        return rc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run(args: argparse.Namespace) -> int:
    prompt = _build_prompt()
    print(f"[build] prompt: {len(prompt):,} chars", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] prompt below; not typed.", file=sys.stderr)
        print(prompt)
        return 0

    return _stage_type(prompt, args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "reprint-markdown",
        help="Ask the AI to reprint all edited markdown files, fully",
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
