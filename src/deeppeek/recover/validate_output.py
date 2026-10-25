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
import sys
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


# ---------------------------------------------------------------------------
# Duplicate-block deduplication
# ---------------------------------------------------------------------------
#
# Duplicate application is one of the two ways a conversation with
# overlapping code blocks produces corrupt output (the other is
# truncation, handled by `repair.py`).  The shape:
#
#   · two adjacent source blocks with similar/identical content
#   · both applied — once via `_try_insert_after_anchor`, once via
#     `_try_structural_append`, once via `_try_context_insert`
#   · result contains the same `if (COND) { ... }` guard twice, or
#     the same `const NAME = ...; if (NAME) { ... }` wiring twice
#
# Detected and removed here, before `validate_content` runs.  If the
# deduped content validates, it's written.  If not, the record
# fail-closes as before — dedup never makes the result worse.
#
# Two classes:
#
#   Class A — duplicate `if (COND) { ... }` blocks
#     Same normalized COND, same indent, within 100 lines of each
#     other, bodies overlap after stripping whitespace.  This is the
#     pg_panel_fields.py shape (guard block inserted twice).
#
#   Class B — duplicate `const NAME = ...;` at module scope
#     Same NAME, indent 0.  Removes the second const and any
#     immediately-following `if (NAME) { ... }`.  This is the
#     pg_panel_scrubs.py shape (wiring pair inserted twice).
#
# Both classes are *always* bugs when the bodies overlap — either a
# real user error (JS would reject the duplicate const; duplicate
# if-conditions are redundant) or, in the recovery context, a
# duplicate-insertion bug.  Removing the second is never worse than
# leaving it.

_IF_OPENER_RE = re.compile(
    r"^([ \t]*)if\s*\((.*)\)\s*(\{)\s*(?://.*)?$", re.MULTILINE)
_MODULE_CONST_RE = re.compile(
    r"^const\s+([A-Za-z_$][\w$]*)\s*=", re.MULTILINE)

_DEDUP_WINDOW_CHARS = 1500


def _enclosing_scope_depth(content: str, pos: int) -> tuple:
    """Return (depth, last_open_pos) at `pos`.  Strings/comments skipped."""
    depth = 0
    stack = []
    i = 0
    in_str = None
    while i < pos:
        c = content[i]
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
        if c == "/" and i + 1 < pos:
            if content[i + 1] == "/":
                j = content.find("\n", i + 2)
                i = pos if j == -1 or j >= pos else j
                continue
            if content[i + 1] == "*":
                j = content.find("*/", i + 2)
                i = pos if j == -1 or j >= pos else j + 2
                continue
        if c == "{":
            depth += 1
            stack.append(i)
        elif c == "}":
            depth -= 1
            if stack:
                stack.pop()
        i += 1
    return depth, (stack[-1] if stack else -1)



def _norm_lines(s: str) -> str:
    """Whitespace-normalized form: all non-blank lines stripped,
    joined by newlines.  Used to compare two blocks for identity
    regardless of indentation."""
    return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())


def _strip_inline_strings(line: str) -> str:
    """Remove single-line quoted strings, JS line comments, and
    single-line JS block comments from a line.  Used for line-based
    brace counting without miscounting braces inside strings or
    comments."""
    out, in_str, i, n = [], None, 0, len(line)
    while i < n:
        c = line[i]
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
        if c == "/" and i + 1 < n and line[i + 1] == "/":
            break
        if c == "/" and i + 1 < n and line[i + 1] == "*":
            j = line.find("*/", i + 2)
            if j != -1:
                i = j + 2
                continue
            break
        out.append(c)
        i += 1
    return "".join(out)
def _find_matching_close_char_linebased(content: str,
                                        open_brace_idx: int) -> int:
    """Line-based brace matcher for Python files with embedded JS.

    Robust against triple-quoted string delimiters by
    processing each line independently — no cross-line string state.
    """
    line_idx_start = content.count("\n", 0, open_brace_idx)
    lines = content.split("\n")
    if line_idx_start >= len(lines):
        return -1
    line_starts = [0]
    for j in range(len(lines) - 1):
        line_starts.append(line_starts[-1] + len(lines[j]) + 1)
    line_start_pos = line_starts[line_idx_start]
    depth = 0
    for i in range(line_idx_start, len(lines)):
        line = lines[i]
        if i == line_idx_start:
            line = line[open_brace_idx - line_start_pos:]
        stripped = _strip_inline_strings(line)
        depth += stripped.count("{") - stripped.count("}")
        if depth == 0 and i > line_idx_start:
            return line_starts[i] + len(lines[i]) - 1
    return -1


def _find_matching_close_char(content: str, open_brace_idx: int,
                              is_python: bool = False) -> int:
    """Return the char index of the `}` matching the `{` at
    `open_brace_idx`, or -1 if unbalanced.  Strings and comments are
    skipped (a `}` inside a string or comment does not close the
    block)."""
    if open_brace_idx >= len(content) or content[open_brace_idx] != "{":
        return -1
    if is_python:
        return _find_matching_close_char_linebased(content, open_brace_idx)
    depth = 1
    i = open_brace_idx + 1
    n = len(content)
    in_str = None
    while i < n:
        c = content[i]
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
            if content[i + 1] == "/":
                j = content.find("\n", i + 2)
                i = n if j == -1 else j
                continue
            if content[i + 1] == "*":
                j = content.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _has_obvious_duplicates(content: str) -> bool:
    """Cheap pre-check: any duplicate `const NAME` or `if (COND)`?"""
    seen_consts = set()
    for m in _MODULE_CONST_RE.finditer(content):
        name = m.group(1)
        if name in seen_consts:
            return True
        seen_consts.add(name)
    seen_ifs = set()
    for m in _IF_OPENER_RE.finditer(content):
        key = (len(m.group(1)),
               re.sub(r"\s+", " ", m.group(2)).strip())
        if key in seen_ifs:
            return True
        seen_ifs.add(key)
    return False


# DEDUP-v2: file-type guard + same-string check for .py

_DEDUP_SAFE_EXTENSIONS = frozenset({
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py",
})


def _python_string_spans(content: str) -> list:
    """Return (start, end) spans of every triple-quoted string literal.

    Used to verify that duplicate blocks in embedded JS are inside the
    SAME Python string (a pipeline artifact) rather than in different
    strings (likely documentation examples)."""
    spans = []
    TRIPLE_D = chr(34) * 3
    TRIPLE_S = chr(39) * 3
    for delim in (TRIPLE_D, TRIPLE_S):
        escaped = re.escape(delim)
        pat = re.compile(
            r"(?:[rbufRBUF]{0,2})" + escaped + r"(.*?)" + escaped,
            re.DOTALL,
        )
        for m in pat.finditer(content):
            spans.append((m.start(), m.end()))
    spans.sort()
    return spans


def _same_python_string(pos_a: int, pos_b: int, spans: list) -> bool:
    """True iff both positions are inside the same triple-quoted string."""
    for start, end in spans:
        if start <= pos_a < end and start <= pos_b < end:
            return True
    return False


def _dedup_duplicate_blocks(content: str, path: str) -> tuple:
    """Remove duplicate-applied blocks.  Returns (new_content, n_removed).

    File-type guard: dedup runs only on code files (.js/.mjs/.cjs/
    .jsx/.ts/.tsx/.py).  For documentation-style files (.md, .html,
    .css, .yaml, .json, .toml, .sh, ...) duplicate const/if lines are
    almost always author-written examples, not pipeline artifacts;
    removing them would corrupt the file.  Skipping those extensions
    trades coverage for safety, which is the correct trade-off.

    Within code files:

      A. Duplicate if-blocks (if (COND) { ... }) with identical
         normalized bodies in the same scope, within
         _DEDUP_WINDOW_CHARS.

      B. Duplicate module-scope const declarations (const NAME = ...),
         plus their trailing if (NAME) { ... } blocks, within
         _DEDUP_WINDOW_CHARS.

    For Python files with embedded JS, "same scope" is approximated
    by "same triple-quoted string literal": a duplicate in a different
    string is treated as an author-written example, not a pipeline
    artifact.  This is a heuristic; a same-string before/after example
    would still be collapsed, but cross-string examples are protected.
    """
    ext = Path(path).suffix.lower()
    if ext not in _DEDUP_SAFE_EXTENSIONS:
        return content, 0
    if not _has_obvious_duplicates(content):
        return content, 0

    is_python = ext == ".py"
    py_spans = _python_string_spans(content) if is_python else None

    removals = []

    # ---- Class A: duplicate if-blocks with same condition ----
    seen_cond = {}
    for m in _IF_OPENER_RE.finditer(content):
        cond = re.sub(r"\s+", " ", m.group(2)).strip()
        if not cond:
            continue
        # m.start(3) is the `{` the regex captured directly — robust
        # against trailing `// comment` on the opener line, where
        # m.end() - 1 would point into the comment.
        open_char = m.start(3)
        close_char = _find_matching_close_char(
            content, open_char, is_python=is_python)
        if close_char == -1:
            continue
        end_char = close_char + 1
        if end_char < len(content) and content[end_char] == "\n":
            end_char += 1

        key = (len(m.group(1)), cond)
        if key in seen_cond:
            prev_start, prev_end = seen_cond[key]
            if m.start() - prev_start > _DEDUP_WINDOW_CHARS:
                seen_cond[key] = (m.start(), end_char)
                continue
            body_a = _norm_lines(content[prev_start:prev_end])
            body_b = _norm_lines(content[m.start():end_char])
            if not body_a or body_a != body_b:
                continue
            if is_python:
                if not _same_python_string(prev_start, m.start(), py_spans):
                    continue
            else:
                if (_enclosing_scope_depth(content, prev_start)
                        != _enclosing_scope_depth(content, m.start())):
                    continue
            removals.append((m.start(), end_char,
                             "if-block (cond: " + cond[:50] + ")"))
            continue
        seen_cond[key] = (m.start(), end_char)

    # ---- Class B: duplicate module-scope const NAME = ----
    seen_const = {}
    for m in _MODULE_CONST_RE.finditer(content):
        if m.start() != content.rfind("\n", 0, m.start()) + 1:
            continue
        name = m.group(1)
        line_end = content.find("\n", m.start())
        if line_end == -1:
            line_end = len(content)
        else:
            line_end += 1
        end_char = line_end
        tail = content[line_end:]
        skip = 0
        while skip < len(tail) and tail[skip] in " \t\n":
            skip += 1
        if tail.startswith("if (" + name + ")", skip):
            brace_rel = tail.find("{", skip)
            if brace_rel != -1:
                close_abs = _find_matching_close_char(
                    content, line_end + brace_rel, is_python=is_python)
                if close_abs != -1:
                    end_char = close_abs + 1
                    if (end_char < len(content)
                            and content[end_char] == "\n"):
                        end_char += 1

        if name in seen_const:
            prev_start, _ = seen_const[name]
            if m.start() - prev_start > _DEDUP_WINDOW_CHARS:
                seen_const[name] = (m.start(), end_char)
                continue
            if is_python:
                if not _same_python_string(prev_start, m.start(), py_spans):
                    continue
            removals.append((m.start(), end_char, "const " + name))
            continue
        seen_const[name] = (m.start(), end_char)

    if not removals:
        return content, 0

    # Merge overlapping removals (Class A and B can both flag the same
    # block; applying overlapping removals in reverse order corrupts
    # the content because positions shift after each removal).
    removals.sort(key=lambda r: r[0])
    merged = [removals[0]]
    for r in removals[1:]:
        prev_start, prev_end, prev_desc = merged[-1]
        if r[0] <= prev_end:
            merged[-1] = (prev_start, max(prev_end, r[1]),
                          prev_desc + " + " + r[2])
        else:
            merged.append(r)
    removals = merged

    removals.sort(key=lambda r: r[0], reverse=True)
    new_content = content
    for rstart, rend, desc in removals:
        print("  [dedup] " + path + ": removing duplicate " + desc,
              file=sys.stderr)
        new_content = new_content[:rstart] + new_content[rend:]
    return new_content, len(removals)