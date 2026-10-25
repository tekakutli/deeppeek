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

This conversation is happening through a GUI that renders
markdown imperfectly. In particular, it mangles nested code
fences. Any file you emit whose content itself contains a
three-backtick fence marker will come back to me as broken
text, and I will not be able to recover the file from the
transcript.

From now on — for every file you write, edit, create, or reprint
in this conversation, including code, markdown, configs, tests,
and documentation — you MUST follow these rules.

RULE 1. Use a quoted bash heredoc.

    python3 - <<'PYEOF'
    ...
    PYEOF

The single quotes around PYEOF disable bash expansion. Without
them, dollar signs, backticks, and quotes inside the body would
be expanded at execution time.

RULE 2. Never put a literal backtick in the heredoc body. Build
every backtick at Python runtime with chr(96):

  - chr(96)        — one backtick
  - chr(96) * 3    — the three-backtick fence marker
  - chr(96) * 4    — a four-backtick fence, if needed

This is the load-bearing rule. If the body contains a raw
three-backtick run, the markdown renderer treats it as a fence
boundary and cuts the message short. chr(96) delays construction
past the renderer's parser.

RULE 3. Wrap the whole bash in a markdown code fence.

Open with a line that is ```bash, close with ``` alone. This
gives me a one-click copy button. If the body must display
three-backtick fences as examples (a doc about markdown, a
fixture containing a fence, a README documenting code-fenced
code), use a four-backtick opening line (````bash) instead.
Rule: outer fence width must exceed the widest inner fence.

RULE 4. Batch files into one heredoc block.

One block, many w(...) calls, one renderer round-trip. Ten w()
calls in one block is strictly safer than ten blocks with one
call each.

RULE 5. Never nest the same triple-quote style in Python
strings.

No triple-single-quote inside a triple-single-quoted string,
and no triple-double-quote inside a triple-double-quoted
string. Use def-based examples (no docstring), flip one side,
or construct via chr(34) * 3 / chr(39) * 3.

RULE 6. Verify before write.

Any string manipulation that produces a .py file: ast.parse
the result before writing it. If parsing fails, do not write.
Read-only probe first, verified write second, idempotent
patches so a re-run is a no-op.

---

Canonical pattern for a single file:

```bash
python3 - <<'PYEOF'
from pathlib import Path
B = chr(96) * 3   # three backticks; chr(96) is the backtick
content = "\\n".join([
    "# heading",
    "",
    B + "python",
    "x = 1",
    B,
])
Path("path/to/file.md").write_text(content)
print("wrote path/to/file.md")
PYEOF
```

Canonical pattern for multiple files in one block:

```bash
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
```

This is a standing order. It applies to every file you emit in
this conversation from this point forward, not only the next
one. If you have already emitted a file that arrived broken
because it contained raw markdown fences or literal backticks,
re-emit it now using the pattern above.
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
