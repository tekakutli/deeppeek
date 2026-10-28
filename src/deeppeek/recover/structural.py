"""structural.py — language-aware balance check for candidate bodies.

`_js_balance` (in validate_output) is a JavaScript-oriented
brace/paren/bracket counter.  It does not understand Python's `#`
comments, so an apostrophe inside a Python comment — for instance
``ComfyUI's`` on a documentation line — opens a string that never
closes, and `_js_balance` reports ``unclosed string literal``.

Applying `_js_balance` unconditionally to a Python body therefore
falsely rejects structurally-valid candidates.  `comfyui.py` in the
2026-10-28 recovery was the motivating case: the new
`comfy_set_layer_pixels` method had a `# ComfyUI's ...` comment in its
mask branch, `_js_balance` reported one unclosed string, and
`_try_indented_def_replace` silently bailed.  The whole replacement
was lost.

`check_balance` dispatches in this order:

  1. If the destination path ends with `.py` OR the body's first
     non-blank line looks like Python (def/class/@/import/from/raise/
     assert or a bare assignment), attempt
     `ast.parse(textwrap.dedent(body))`.  A successful parse is
     ground truth: this body IS Python, and any apostrophe in a `#`
     comment is just a comment character.  Return None (valid).

  2. Otherwise — the parse failed, or the body doesn't look Python —
     fall back to `_js_balance`.  The body may be JS/HTML/CSS, either
     as a standalone destination or embedded in a `.py` string.

`check_balance` returns None on success, or a short reason string on
failure.  It never raises.
"""

from __future__ import annotations

import ast
import re
import textwrap
from typing import Optional

from .validate_output import _js_balance


_ASSIGNMENT_RE = re.compile(
    r"^[A-Za-z_][\w]*\s*=\s*(?:[{\[(\"']|[^=])"
)


def _looks_like_python(body: str) -> bool:
    """True if the first non-blank line reads as Python.

    A hint only — `ast.parse` is the ground truth.  Returns False for
    a JS `const x = 1;` because the pattern requires ``ident =`` not
    ``const ident =``.
    """
    for line in body.splitlines():
        s = line.lstrip()
        if not s:
            continue
        if (s.startswith("def ")
                or s.startswith("async def ")
                or s.startswith("class ")
                or s.startswith("@")):
            return True
        if s.startswith(("import ", "from ", "raise ", "assert ")):
            return True
        if _ASSIGNMENT_RE.match(s):
            return True
        return False
    return False


def check_balance(body: str, path: str = "") -> Optional[str]:
    """Return None when `body` is structurally balanced, else a short
    reason string.  Never raises.  See the module docstring."""
    if not body or not body.strip():
        return None

    if path.endswith(".py") or _looks_like_python(body):
        try:
            ast.parse(textwrap.dedent(body))
        except SyntaxError:
            pass
        else:
            return None

    b, p, k, u = _js_balance(body)
    if b != 0:
        return f"unbalanced braces ({b:+d})"
    if p != 0:
        return f"unbalanced parens ({p:+d})"
    if k != 0:
        return f"unbalanced brackets ({k:+d})"
    if u:
        return "unclosed string literal"
    return None
