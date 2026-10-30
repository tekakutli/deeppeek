"""Comment-span detection for the anchor heuristics.

An anchor match that falls inside a comment is never a valid insertion
point.  Placing a body after a line that is part of a `/* ... */` block
comment, a `//` line comment, a Python `#` comment, or a triple-quoted
docstring produces a fragment that is *lexically valid* (comments don't
affect the enclosing syntax) but *semantically broken* — the new code
never executes.

Motivating case (2026-10-28d): the pg_panel_scrubs.py
`_clearImageAction` fragment (block idx=72) was placed after the text
`pgkitNotifyAll() there already includes this field.` — a line inside
the `/* ... */` docstring-block comment for `masks.selectedIndex`.  The
file compiled, the AST parsed, the record reconciled to MEDIUM, and
the fragment was dead code in a comment.

For `.py` files, BOTH Python syntax (`#` comments, triple-quoted
strings) and JS syntax (`//` line comments, `/* */` block comments)
are detected, and JS comments inside triple-quoted strings are
reported as comment spans in the outer file's coordinate space.  This
is what makes the playground `.py` files (JS-in-`r\"\"\"...\"\"\"`) work
correctly: an anchor that matches inside a JS block comment is
correctly refused, even though the anchor's character position is
inside a Python string from the outer file's perspective.

For `.js` / `.mjs` / `.cjs` / `.jsx` / `.ts` / `.tsx` files, only JS
syntax is detected.

For unknown extensions, both are attempted on the principle that
over-detection is safe: a span incorrectly flagged as a comment only
causes an anchor to be skipped, falling through to the next anchor or
the caller's failure path.  Under-detection is the dangerous direction
(content gets placed inside a comment) and every realistic comment
style is covered.
"""

from __future__ import annotations

import re


_JS_LANGS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")


# ============================================================================
#  Public API
# ============================================================================

def get_comment_spans(text: str, path: str = "") -> list:
    """Return `[(start_char, end_char)]` spans of comments in `text`.

    `path`'s extension determines the syntaxes detected.  See module
    docstring.  Spans are non-overlapping and sorted.
    """
    is_py = path.endswith(".py")
    is_js = any(path.endswith(ext) for ext in _JS_LANGS)

    if is_js and not is_py:
        return _sorted_dedup(_find_js_comments(text))

    if is_py:
        spans = _find_python_comments(text)
        for content_start, content in _iter_triple_quoted(text):
            for s, e in _find_js_comments(content):
                spans.append((content_start + s, content_start + e))
        return _sorted_dedup(spans)

    # Unknown extension: try both, on the safe-over-detection principle.
    spans = _find_python_comments(text) + _find_js_comments(text)
    return _sorted_dedup(spans)


def is_inside_comment(pos: int, spans: list) -> bool:
    """True iff `pos` falls inside any span in `spans`."""
    for start, end in spans:
        if start <= pos < end:
            return True
    return False


# ============================================================================
#  Internals
# ============================================================================

def _sorted_dedup(spans: list) -> list:
    """Sort and deduplicate (nested spans collapse to the outer one)."""
    if not spans:
        return []
    spans.sort()
    out = [spans[0]]
    for start, end in spans[1:]:
        prev_start, prev_end = out[-1]
        if start <= prev_end:
            out[-1] = (prev_start, max(prev_end, end))
        else:
            out.append((start, end))
    return out


def _find_js_comments(text: str) -> list:
    """`//` line comments and `/* ... */` block comments in JS."""
    spans = []
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'", chr(96)):
            in_str = c
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                end = text.find("\n", i)
                if end == -1:
                    end = n
                spans.append((i, end))
                i = end
                continue
            if text[i + 1] == "*":
                close = text.find("*/", i + 2)
                if close == -1:
                    spans.append((i, n))
                    i = n
                else:
                    spans.append((i, close + 2))
                    i = close + 2
                continue
        i += 1
    return spans


def _find_python_comments(text: str) -> list:
    """`#` comments at Python scope, skipping strings."""
    spans = []
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            # Triple-quote: skip as a block.
            if i + 2 < n and text[i:i + 3] == c * 3:
                close = text.find(c * 3, i + 3)
                i = n if close == -1 else close + 3
                continue
            in_str = c
            i += 1
            continue
        if c == "#":
            end = text.find("\n", i)
            if end == -1:
                end = n
            spans.append((i, end))
            i = end
            continue
        i += 1
    return spans


_TRIPLE_QUOTED_RE = re.compile(
    r'(?:[rbufRBUF]{0,2})("""|\'\'\')(.*?)\1', re.DOTALL)


def _iter_triple_quoted(text: str):
    """Yield `(content_start, content)` for each triple-quoted string.

    Fast-path + precompiled regex.  Handles Python string prefixes
    (`r`, `rb`, `f`, `u`, etc.).  Does not handle escapes inside
    the triple quotes; a `\\\"\\\"\\\"` inside a triple-quoted string
    is rare and, when it does appear, the resulting over-detected
    comment span is safe (see module docstring).
    """
    if '"""' not in text and "'''" not in text:
        return
    for m in _TRIPLE_QUOTED_RE.finditer(text):
        yield m.start(2), m.group(2)
