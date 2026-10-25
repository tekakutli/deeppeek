"""Post-apply structural validation.

`pass8_verify` runs `ast.parse` for `.py` files, but that only
validates the OUTER Python.  Playground and template projects embed
JS/HTML inside raw triple-quoted string literals; the outer Python
parses fine, but the embedded JS can be broken.

This module inspects the actual content the patcher produced and
detects the failure classes that matter for those projects:

  1. Function-presence regression - a function/class/const declared
     in the base is missing from the result.  Catches a scoped-replace
     that deleted its target instead of replacing it.

  2. Brace/paren/bracket balance - for JS content (standalone files
     and embedded inside `.py` strings).  Catches structural appends
     that inserted an unbalanced block.

  3. Duplicate top-level declaration - for JS content.  Catches the
     same block being applied twice.

  4. `<details>` element count regression - for HTML content inside
     `.py` strings.  Catches a scoped replace that silently dropped
     an entire section.

All checks return None on success, or a one-line string describing
the failure.  Any non-None return means the file must be fail-closed
(LOW trust -> low_trust/) so the target tree is preserved.
"""

import ast
import re
from pathlib import Path
from typing import Optional


_JS_EXTS = frozenset({".js", ".mjs", ".jsx", ".ts", ".tsx"})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _js_balance(text: str) -> tuple:
    """Return (braces, parens, brackets, strings_unclosed).

    Skips content inside strings and comments.  Nonzero means unbalanced.
    """
    braces = parens = brackets = 0
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
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
                j = text.find("\n", i + 2)
                i = n if j == -1 else j
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        if c == "{":
            braces += 1
        elif c == "}":
            braces -= 1
        elif c == "(":
            parens += 1
        elif c == ")":
            parens -= 1
        elif c == "[":
            brackets += 1
        elif c == "]":
            brackets -= 1
        i += 1
    return braces, parens, brackets, (1 if in_str else 0)


# ---------------------------------------------------------------------------
# Function / declaration presence
# ---------------------------------------------------------------------------

_DECL_PATTERNS = (
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
        re.MULTILINE),
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
        re.MULTILINE),
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?class\s+([A-Za-z_$][\w$]*)",
        re.MULTILINE),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
        re.MULTILINE),
)


def _declared_names(text: str) -> set:
    """Return every declaration name found in text (any language)."""
    out = set()
    for pat in _DECL_PATTERNS:
        for m in pat.finditer(text):
            out.add(m.group(1))
    return out


def _function_presence_check(base: str, result: str,
                              label: str,
                              allow_missing: Optional[set] = None,
                              ) -> Optional[str]:
    """Every top-level name declared in base must appear in result,
    unless it is listed in `allow_missing` (declarations that a
    deletion patch was explicitly instructed to remove)."""
    if not base:
        return None
    base_names = _declared_names(base)
    if not base_names:
        return None
    result_names = _declared_names(result)
    missing = base_names - result_names
    if allow_missing:
        missing = missing - set(allow_missing)
    if missing:
        listed = sorted(missing)[:6]
        return (f"{label}: declaration(s) present in base but missing "
                f"from result: {listed}"
                + (" ..." if len(missing) > 6 else ""))
    return None


# ---------------------------------------------------------------------------
# JS structural checks
# ---------------------------------------------------------------------------

def _js_structural_check(text: str, label: str) -> Optional[str]:
    braces, parens, brackets, unclosed = _js_balance(text)
    if braces != 0:
        return f"{label}: unbalanced braces ({braces:+d})"
    if parens != 0:
        return f"{label}: unbalanced parens ({parens:+d})"
    if brackets != 0:
        return f"{label}: unbalanced brackets ({brackets:+d})"
    if unclosed:
        return f"{label}: unclosed string literal"
    # Column-0 declarations only.  JS `const` is block-scoped, so
    # `const res = ...` inside `uploadImageFile` and another
    # `const res = ...` inside `fetchStatus` are distinct bindings,
    # not a duplicate.  Only unindented declarations at the top level
    # of the JS file are true duplicates.  An earlier version of
    # this check used `^\s*`, which flagged every local variable
    # declared twice — the false-positive class that rejected
    # pg_state.py, pg_pixels.py, pg_dispatch.py, pg_panel_bindings.py,
    # pg_view_image.py, pg_comfy_client.py, and pg_krita_client.py
    # on the conversation that revealed the bug.
    decls = re.findall(
        r"^(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
        text, re.MULTILINE)
    seen = {}
    for d in decls:
        if d in seen:
            return f"{label}: duplicate top-level declaration '{d}'"
        seen[d] = True
    return None


# ---------------------------------------------------------------------------
# HTML structural checks
# ---------------------------------------------------------------------------

_HTML_TAG_PAIRS = (
    ("details", "</details>", "<details"),
    ("div", "</div>", "<div"),
    ("button", "</button>", "<button"),
)


def _html_structural_check(text: str, label: str) -> Optional[str]:
    for name, close_tag, open_re in _HTML_TAG_PAIRS:
        opens = len(re.findall(re.escape(open_re) + r"\b", text))
        closes = text.count(close_tag)
        if opens != closes:
            return (f"{label}: {opens} <{name}> vs {closes} </{name}>")
    braces, parens, brackets, _ = _js_balance(text)
    if braces != 0:
        return f"{label}: unbalanced braces in embedded HTML ({braces:+d})"
    return None


# ---------------------------------------------------------------------------
# Extract embedded triple-quoted strings
# ---------------------------------------------------------------------------

def _iter_triple_quoted(content: str):
    for m in re.finditer(
            r"(?:[rbufRBUF]{0,2})" + chr(34) * 3
            + r"(.*?)" + chr(34) * 3,
            content, re.DOTALL):
        yield m.start(1), m.group(1)
    for m in re.finditer(
            r"(?:[rbufRBUF]{0,2})" + chr(39) * 3
            + r"(.*?)" + chr(39) * 3,
            content, re.DOTALL):
        yield m.start(1), m.group(1)


def _looks_like_js(text: str) -> bool:
    signals = 0
    if re.search(r"\bexport\s+(?:function|const|class)\b", text):
        signals += 2
    if re.search(r"\bfunction\s+\w+\s*\(", text):
        signals += 1
    if re.search(r"\bconst\s+\w+\s*=", text):
        signals += 1
    if "=>" in text:
        signals += 1
    if "document." in text:
        signals += 1
    return signals >= 2


def _looks_like_html(text: str) -> bool:
    return ("<details" in text or "</details>" in text
            or "<button" in text)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_content(content: str, path: str,
                     base: Optional[str] = None,
                     allow_missing: Optional[set] = None,
                     ) -> Optional[str]:
    """Return None if content is structurally OK, else an error string."""
    if content is None:
        return f"{path}: content is None"
    if not content.strip():
        return f"{path}: content is empty"

    ext = Path(path).suffix.lower()

    if ext in _JS_EXTS:
        err = _js_structural_check(content, path)
        if err:
            return err
        if base:
            return _function_presence_check(
                base, content, path, allow_missing=allow_missing)
        return None

    if ext == ".py":
        try:
            ast.parse(content)
        except SyntaxError as e:
            return (f"{path}: python SyntaxError at line {e.lineno}: "
                    f"{e.msg}")

        for offset, block in _iter_triple_quoted(content):
            lineno = _line_of(content, offset)
            label = f"{path} (embedded at line {lineno})"
            if _looks_like_html(block):
                err = _html_structural_check(block, label)
                if err:
                    return err
            elif _looks_like_js(block):
                err = _js_structural_check(block, label)
                if err:
                    return err

        if base:
            return _function_presence_check(
                base, content, path, allow_missing=allow_missing)
        return None

    return None
