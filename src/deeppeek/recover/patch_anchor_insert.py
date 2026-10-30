"""
patch_anchor_insert — insert relative to a named anchor line (FIX 3).

Four heuristics that place a body at a *position* named by prose
("before `X`", "between `X` and `Y`", "after `X`"):

    _try_insert_before_anchor     "before the `X` field"
    _try_insert_between_anchors   "between the `Image` and `Masks` blocks"
    _try_insert_after_anchor      "after `some_code_call();`"
    _try_insert_after_def         (helper: insert after a named
                                   def/class/function's whole body)

DEF-ANCHOR (2026-10-28)
=======================
`_try_insert_after_anchor` gained a branch for backticked identifiers
that name a def/class/function.

Multi-line signatures (`def foo(\n a,\n b,\n):`) are handled by
`_find_python_sig_end`.

PRIOR-INSERTIONS FALLBACK (2026-10-28)
======================================
When the prose names no backtick anchor at all, but an earlier patch
in the same batch already inserted a line that appears (uniquely) in
the current base, insert after that line.

SCOPE-FIRST ORDERING (2026-10-28f)
==================================
Prior-insertion fallback partitions `prior_insertions` into **scoped**
(lines with a `prior_scopes` entry) and **unscoped**, tries all
scoped candidates before any unscoped one.

SAFETY GATES ON PRIOR-INSERTION ANCHORS
=======================================
Two gates:
  · `_anchor_line_is_safe`: refuse a line whose own text opens more
    brackets than it closes.
  · `_anchor_is_in_expression_context`: for `.py` files, refuse a
    line whose position lies inside any unclosed `(`/`[`/`{`.

ANCHOR-LINE SAFETY (Fix 4)
==========================
`_anchor_line_is_safe` refuses anchors that open an unclosed block.

MULTI-OCCURRENCE ANCHORS (Fix 5)
================================
`_try_insert_before_anchor` tries occurrences right-to-left, skipping
closers-only lines and non-code lines.

SCOPE-AWARE ANCHOR SELECTION (2026-10-28b)
==========================================
When an anchor is non-unique, look for a unique occurrence inside the
prose-named scope.

CALL-ONLY ANCHORS FALL THROUGH (2026-10-28c)
============================================
`func()` shapes fall through to the statement branch.

COMMENT-AWARE ANCHORS (2026-10-28d)
===================================
Every anchor branch consults `patch_comments.get_comment_spans(base,
path)` and skips any candidate position inside a comment.

FILENAME ANCHORS REJECTED (2026-10-29)
======================================
A prose backtick that resolves to a *filename* (`` `pg_base.py` ``,
`` `some_module.js` ``, `` `data/config.toml` ``) is a reference to
the destination file, not a code anchor.  Without a guard, the
statement branch captures the filename (it passes the length and
identifier checks), finds its unique occurrence in base — typically
inside the module docstring, where the file names itself — and
splices the patch body there.

Motivating case: `pg_base.py`, patch 636 (HTML rotation widget).
Prose is "In `pg_base.py`, replace the rotation field:".  The anchor
`pg_base.py` matches line 2 of the module docstring; the HTML body
gets spliced into the docstring, which is valid Python (a string
literal), so every check (`ast.parse`, `validate_content`,
`patch_presence`) reports success.  The file's `PG_PANEL_BODY` never
receives the widget.

`_FILENAME_ANCHOR_RE` rejects anchors whose whole text resolves to a
source-file path.

CLASS-TARGET + DECORATED-BODY (2026-11-02)
==========================================
`_try_insert_after_def` refuses a `@`-decorated body when `ident`
names a `class` in base.  Inserting after a class places the
decorated def at module level — semantically wrong (the def is a
method, not a module-level function).  The scope-insert heuristic
(`_try_insert_into_named_scope`) is the correct handler for
class+decorated-body; if it declines for some reason, this branch
declining lets the pipeline fall through to structural-append (which
also declines class+decorated), and finally to fail-closed — a
strictly better outcome than placing the method at module level.

AST-BASED SCOPE RESOLUTION (2026-11-05)
=======================================
`_try_insert_after_def` now resolves the def/class/function's span
via `scope_ast.find_scope` instead of its own two regexes + brace
walker.  The AST path handles every declaration shape the language
defines (decorated defs, `async def`, generator functions, arrow
functions assigned to `const`, class methods, HTML `<details>`),
with exact boundaries.

The scope-aware anchor helpers (`_anchor_within_scope`,
`_anchor_within_named_scope`, `_anchor_within_callee`) likewise
delegate to `patch_scope_insert._find_scope_body_range`, which is
now AST-backed, and take a `path` argument that they thread
through so the destination language's parser is chosen correctly.

Behaviour of every heuristic on the previously-supported shapes is
unchanged; the only visible difference is that shapes the regex
missed (arrow functions, generator functions, class methods) now
resolve instead of failing closed.
"""

import re

from .patch_predicates import (
    _body_is_complete_at_boundary,
    _anchor_line_is_safe,
    _line_is_closer_only,
    _line_has_code_signal,
)
from .patch_text import (
    _find_matching_brace,
    _find_matching_paren,
    _reindent_body,
    _find_identifier_block_end_line,
)
from .patch_comments import get_comment_spans, is_inside_comment


_BEFORE_PROSE_RE = re.compile(
    r'\bbefore\b[^.\n]{0,120}?`([^`\n]+)`',
    re.IGNORECASE,
)


def _try_insert_before_anchor(base, body, context_before, path=""):
    if not body or not body.strip():
        return None
    body_stripped = body.strip()
    if not _body_is_complete_at_boundary(body_stripped):
        return None

    matches = _BEFORE_PROSE_RE.findall(context_before or "")
    if not matches:
        return None

    spans = get_comment_spans(base, path)

    for anchor_text in reversed(matches):
        anchor_text = anchor_text.strip()
        if len(anchor_text) < 6:
            continue

        positions = []
        start = 0
        while True:
            p = base.find(anchor_text, start)
            if p == -1:
                break
            if not is_inside_comment(p, spans):
                positions.append(p)
            start = p + 1
        if not positions:
            continue

        for idx in reversed(positions):
            line_start = base.rfind("\n", 0, idx) + 1
            line_end = base.find("\n", line_start)
            if line_end == -1:
                line_end = len(base)
            line = base[line_start:line_end]

            if _line_is_closer_only(line):
                continue
            if not _line_has_code_signal(line):
                continue

            indent = line[:len(line) - len(line.lstrip())]
            reindented = _reindent_body(indent, body_stripped)
            insertion = reindented.rstrip() + "\n"
            result = base[:line_start] + insertion + base[line_start:]
            if result != base:
                return result

    return None


_BETWEEN_RE = re.compile(
    r'\bbetween\b[^.\n]{0,120}?'
    r'(?:the\s+)?([A-Za-z][\w ]{1,50}?)\s+(?:and|&)\s+'
    r'(?:the\s+)?([A-Za-z][\w ]{1,50}?)\s+'
    r'(?:<[^>]+>|blocks?|sections?|fields?|functions?|elements?)',
    re.IGNORECASE,
)


def _find_named_block_end(base, name):
    """Return `(start_char, end_char)` for the named declaration,
    or None.

    AST-based since 2026-11-05.  **No `path` parameter on purpose:**
    between-anchors prose names a *block* ("between the Image and
    Masks `<details>` blocks") without naming its language, so the
    lookup must try all three parsers.  Pre-fix, the old regex tried
    `<details>` unconditionally before touching `def`/`function`; the
    AST path reproduces that by passing the empty path, which
    `find_scope` treats as Python → JS → HTML fallback.

    For a `.py` file whose base contains `<details><summary>Image
    </summary>...</details>` inside a raw string, `find_scope(base,
    "Image", "")` tries `_find_python_scope` first (no `Image`
    def/class), then `_find_js_scope` (no `Image` function/const),
    then `_find_html_scope` (matches).  Threading the outer file's
    `.py` path would stop at the Python attempt and return None,
    breaking between-anchors for the playground pattern.
    """
    if not name:
        return None
    from .scope_ast import find_scope
    span = find_scope(base, name, "")
    if span is None:
        return None
    return (span[0], span[1])


def _try_insert_between_anchors(base, body, context_before, path=""):
    if not body or not body.strip():
        return None
    body_stripped = body.strip()
    if not _body_is_complete_at_boundary(body_stripped):
        return None

    cleaned = (context_before or "").replace("`", "")
    m = _BETWEEN_RE.search(cleaned)
    if not m:
        return None

    name_a = m.group(1).strip()
    name_b = m.group(2).strip()
    if not name_a or not name_b or name_a == name_b:
        return None

    range_a = _find_named_block_end(base, name_a)
    range_b = _find_named_block_end(base, name_b)
    if range_a is None or range_b is None:
        return None

    spans = get_comment_spans(base, path)
    if (is_inside_comment(range_a[0], spans)
            or is_inside_comment(range_b[0], spans)):
        return None

    if range_a[1] <= range_b[0]:
        insert_at_char = range_b[0]
    elif range_b[1] <= range_a[0]:
        insert_at_char = range_a[0]
    else:
        return None

    line_start = base.rfind("\n", 0, insert_at_char) + 1
    line_end = base.find("\n", line_start)
    if line_end == -1:
        line_end = len(base)
    line = base[line_start:line_end]
    indent = line[:len(line) - len(line.lstrip())]
    reindented = _reindent_body(indent, body_stripped)
    insertion = reindented.rstrip() + "\n\n"
    result = base[:line_start] + insertion + base[line_start:]
    return result if result != base else None


def _find_python_sig_end(base, start_pos):
    """Kept for tests / external callers.  The 2026-11-05 AST-based
    `_try_insert_after_def` no longer uses this helper directly, but
    downstream code (and unit tests) may still import it.
    """
    i = start_pos
    n = len(base)
    depth = 0
    in_str = None
    while i < n:
        c = base[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = c
            i += 1
            continue
        if c == "#":
            nl = base.find("\n", i)
            i = nl if nl != -1 else n
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ":" and depth == 0:
            return i + 1
        i += 1
    return -1


_DECL_START_PREFIXES = (
    "def ", "async def ", "class ",
    "function ", "async function ", "export ",
    "@",
)


def _try_insert_after_def(base, ident, body, path=""):
    """If `ident` names a def/class/function in `base`, insert `body`
    AFTER the definition's whole body — at the def's own indent.

    Refuses when the body is `@`-decorated and `ident` names a
    `class` in base: the intent is to add a method inside the class,
    not a module-level function after it.  See module docstring,
    CLASS-TARGET + DECORATED-BODY.

    AST-based since 2026-11-05: the span comes from
    `scope_ast.find_scope`.
    """
    if not ident or not body or not body.strip():
        return None

    body_lines = body.lstrip("\n").rstrip().split("\n")
    first_body_line = next(
        (ln for ln in body_lines if ln.strip()), "")
    first_stripped = first_body_line.lstrip()
    if not first_stripped.startswith(_DECL_START_PREFIXES):
        return None

    if first_stripped.startswith("@"):
        _class_re = re.compile(
            r'(?m)^[ \t]*class\s+' + re.escape(ident) + r'\b')
        if _class_re.search(base):
            return None

    from .scope_ast import find_scope
    span = find_scope(base, ident, path)
    if span is None:
        return None
    start_char, end_char, kind = span

    if kind == "python":
        return _insert_after_python_scope(
            base, start_char, end_char, body)
    if kind == "js":
        return _insert_after_js_scope(
            base, start_char, end_char, body)
    return None


def _insert_after_python_scope(base, start_char, end_char, body):
    """Insert `body` after a Python scope, skipping past trailing
    blank lines to match the pre-AST behaviour.
    """
    decl_line_start = base.rfind("\n", 0, start_char) + 1
    decl_line_end = base.find("\n", decl_line_start)
    if decl_line_end == -1:
        decl_line_end = len(base)
    decl_line = base[decl_line_start:decl_line_end]
    def_indent = decl_line[:len(decl_line) - len(decl_line.lstrip())]

    scan = end_char
    while scan < len(base):
        nl = base.find("\n", scan)
        if nl == -1:
            break
        if base[scan:nl].strip():
            break
        scan = nl + 1
    insert_at = scan

    reindented = _reindent_body(
        def_indent, body.lstrip("\n").rstrip())
    insertion = reindented.rstrip() + "\n\n"
    result = base[:insert_at] + insertion + base[insert_at:]
    return result if result != base else None


def _insert_after_js_scope(base, start_char, end_char, body):
    """Insert `body` after a JS scope (right after the `}` line)."""
    line_start = base.rfind("\n", 0, start_char) + 1
    line_end = base.find("\n", line_start)
    if line_end == -1:
        line_end = len(base)
    decl_line = base[line_start:line_end]
    sibling_indent = decl_line[:len(decl_line) - len(decl_line.lstrip())]

    reindented = _reindent_body(
        sibling_indent, body.lstrip("\n").rstrip())
    insertion = reindented.rstrip() + "\n"
    if end_char > 0 and base[end_char - 1] != "\n":
        insertion = "\n" + insertion
    result = base[:end_char] + insertion + base[end_char:]
    return result if result != base else None


def _anchor_within_scope(base, anchor_text, scope_name, spans=None,
                          path=""):
    if not scope_name:
        return -1
    from .patch_scope_insert import _find_scope_body_range
    scope_range = _find_scope_body_range(base, scope_name, path)
    if scope_range is None:
        return -1
    scope_start, scope_end, _ = scope_range

    positions = []
    start = scope_start
    while True:
        p = base.find(anchor_text, start, scope_end)
        if p == -1:
            break
        if spans is None or not is_inside_comment(p, spans):
            positions.append(p)
        start = p + 1
    if len(positions) != 1:
        return -1
    return positions[0]


def _anchor_within_named_scope(base, anchor_text, context_before,
                                spans=None, path=""):
    from .patch_scope_insert import _extract_scope_name_from_prose
    named_scope = _extract_scope_name_from_prose(context_before or "")
    return _anchor_within_scope(
        base, anchor_text, named_scope, spans=spans, path=path)


_CALLEE_CALL_RE = re.compile(r'self\.([A-Za-z_]\w*)\s*\(')


def _anchor_within_callee(base, anchor_text, scope_name, spans=None,
                           path=""):
    if not scope_name:
        return -1
    from .patch_scope_insert import _find_scope_body_range
    scope_range = _find_scope_body_range(base, scope_name, path)
    if scope_range is None:
        return -1
    scope_start, scope_end, _ = scope_range
    scope_body = base[scope_start:scope_end]

    callee_names = set(_CALLEE_CALL_RE.findall(scope_body))
    if not callee_names:
        return -1

    matches = []
    for name in sorted(callee_names):
        callee_range = _find_scope_body_range(base, name, path)
        if callee_range is None:
            continue
        callee_start, callee_end, _ = callee_range
        positions = []
        start = callee_start
        while True:
            p = base.find(anchor_text, start, callee_end)
            if p == -1:
                break
            if spans is None or not is_inside_comment(p, spans):
                positions.append(p)
            start = p + 1
        if len(positions) == 1:
            matches.append((name, positions[0]))

    if len(matches) == 1:
        return matches[0][1]
    return -1


def _anchor_is_in_expression_context(base, pos, path):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in ("py", "pyi"):
        return False
    parens = braces = brackets = 0
    in_str = None
    in_triple = None
    i = 0
    while i < pos:
        c = base[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if in_triple:
            if base.startswith(in_triple, i):
                in_triple = None
                i += 3
                continue
            i += 1
            continue
        if base.startswith('"""', i) or base.startswith("'''", i):
            in_triple = base[i:i+3]
            i += 3
            continue
        if c in ('"', "'"):
            in_str = c
            i += 1
            continue
        if c == "#":
            nl = base.find("\n", i)
            i = nl if nl != -1 else pos
            continue
        if c == "(":
            parens += 1
        elif c == ")":
            parens -= 1
        elif c == "{":
            braces += 1
        elif c == "}":
            braces -= 1
        elif c == "[":
            brackets += 1
        elif c == "]":
            brackets -= 1
        i += 1
    return parens > 0 or braces > 0 or brackets > 0


_PROSE_BACKTICK_IDENT_RE = re.compile(r'`([A-Za-z_$][\w$]*)`')
_PROSE_CALL_ARG_RE = re.compile(
    r'`([A-Za-z_$][\w$]*)\s*\(\s*["\']([^"\']+)["\']')
_BACKTICK_CONTENT_RE = re.compile(r'`([^`\n]+)`')
_IDENT_ONLY_RE = re.compile(r'^[A-Za-z_$][\w$]*$')
_CALL_ONLY_RE = re.compile(r'^[A-Za-z_$][\w$]*\s*\(')


# A backtick content that resolves to a source-file path is a prose
# reference to the destination file, not a code anchor.  See module
# docstring, FILENAME ANCHORS REJECTED (2026-10-29).
_FILENAME_ANCHOR_RE = re.compile(
    r'^(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+'
    r'\.(py|js|mjs|ts|tsx|jsx|html|css|json|yaml|yml|toml|md|sh|rs|go|rb|php)$'
)


def _try_insert_after_anchor(base, body, context_before,
                              prior_insertions=None, path="",
                              prior_scopes=None):
    body_stripped = body.strip()
    if not body_stripped:
        return None
    if not _body_is_complete_at_boundary(body_stripped):
        return None
    body_lines = body_stripped.splitlines()
    if len(body_lines) > 40:
        return None

    spans = get_comment_spans(base, path)

    if len(body_lines) >= 2:
        call_matches = _PROSE_CALL_ARG_RE.findall(context_before or "")
        for func_name, arg_str in reversed(call_matches):
            pattern_str = func_name + '("' + arg_str
            idx = base.find(pattern_str)
            if idx == -1:
                continue
            if is_inside_comment(idx, spans):
                continue
            paren_open = base.find("(", idx + len(func_name))
            if paren_open == -1:
                continue
            paren_close = _find_matching_paren(base, paren_open)
            if paren_close == -1:
                continue
            line_end = base.find("\n", paren_close)
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1
            insertion = body_stripped.rstrip() + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

        ident_matches = _PROSE_BACKTICK_IDENT_RE.findall(
            context_before or "")
        for ident in reversed(ident_matches):
            def_result = _try_insert_after_def(
                base, ident, body, path=path)
            if def_result is not None:
                return def_result

            if_re = re.compile(
                r'(?:^|\n)\s*if\s*\(\s*' + re.escape(ident) + r'\s*\)\s*\{',
                re.MULTILINE)
            if_match = if_re.search(base)
            if if_match and not is_inside_comment(if_match.start(), spans):
                block_open = base.rfind("{", if_match.start(),
                                        if_match.end())
                if block_open == -1:
                    block_open = base.find("{", if_match.start())
                if block_open == -1:
                    continue
                block_close = _find_matching_brace(
                    base, block_open, is_python=False)
                if block_close == -1:
                    continue
                line_end = base.find("\n", block_close)
                if line_end == -1:
                    line_end = len(base)
                else:
                    line_end += 1
                insertion = body_stripped.rstrip() + "\n"
                result = base[:line_end] + insertion + base[line_end:]
                if result != base:
                    return result
                continue

            const_re = re.compile(
                r'(?:^|\n)\s*(?:const|let|var)\s+' + re.escape(ident)
                + r'\s*=[^\n]*;', re.MULTILINE)
            const_match = const_re.search(base)
            if const_match and not is_inside_comment(
                    const_match.start(), spans):
                line_end = base.find("\n", const_match.end())
                if line_end == -1:
                    line_end = len(base)
                else:
                    line_end += 1
                extended_idx = _find_identifier_block_end_line(base, ident)
                if extended_idx is not None:
                    base_lines_local = base.split("\n")
                    extended_offset = sum(
                        len(base_lines_local[k]) + 1
                        for k in range(extended_idx + 1))
                    if (extended_offset > line_end
                            and extended_offset <= len(base)):
                        line_end = extended_offset
                insertion = body_stripped.rstrip() + "\n"
                result = base[:line_end] + insertion + base[line_end:]
                if result != base:
                    return result

        statement_matches = _BACKTICK_CONTENT_RE.findall(
            context_before or "")
        for raw in reversed(statement_matches):
            stripped = raw.strip()
            if len(stripped) < 6:
                continue
            if _IDENT_ONLY_RE.match(stripped):
                continue
            # NEW (2026-10-29): skip filename-shaped anchors.
            # `pg_base.py` from "In `pg_base.py`, replace…" is a
            # reference to the destination file, not a code anchor.
            # Without this guard the heuristic matches the first
            # occurrence of the filename in base — typically inside
            # the module docstring, where the file names itself — and
            # splices the body into the docstring.
            if _FILENAME_ANCHOR_RE.match(stripped):
                continue
            positions = []
            start = 0
            while True:
                p = base.find(stripped, start)
                if p == -1:
                    break
                if not is_inside_comment(p, spans):
                    positions.append(p)
                start = p + 1

            if len(positions) == 1:
                idx = positions[0]
            elif len(positions) > 1:
                idx = _anchor_within_named_scope(
                    base, stripped, context_before, spans=spans,
                    path=path)
                if idx == -1:
                    continue
            else:
                continue

            line_start = base.rfind("\n", 0, idx) + 1
            line_lead = ""
            p_scan = line_start
            while p_scan < len(base) and base[p_scan] in " \t":
                line_lead += base[p_scan]
                p_scan += 1
            anchor_indent = line_lead

            if not _anchor_line_is_safe(base, line_start):
                continue

            line_end = base.find("\n", idx)
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1

            reindented = _reindent_body(
                anchor_indent, body.lstrip("\n").rstrip())
            insertion = reindented.rstrip() + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

    if prior_insertions:
        scoped_priors: list = []
        unscoped_priors: list = []
        for prior in prior_insertions:
            stripped = prior.strip()
            if not stripped or len(stripped) < 8:
                continue
            if prior_scopes and stripped in prior_scopes:
                scoped_priors.append(stripped)
            else:
                unscoped_priors.append(stripped)

        for stripped in sorted(scoped_priors):
            scope = prior_scopes[stripped]
            idx = _anchor_within_scope(
                base, stripped, scope, spans=spans, path=path)
            if idx == -1:
                idx = _anchor_within_callee(
                    base, stripped, scope, spans=spans, path=path)
            if idx == -1:
                continue
            if is_inside_comment(idx, spans):
                continue
            line_start = base.rfind("\n", 0, idx) + 1
            if not _anchor_line_is_safe(base, line_start):
                continue
            anchor_indent = base[line_start:idx]
            line_end = base.find("\n", idx)
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1
            reindented = _reindent_body(
                anchor_indent, body.lstrip("\n").rstrip())
            insertion = reindented.rstrip() + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

        for stripped in sorted(unscoped_priors):
            if base.count(stripped) != 1:
                continue
            idx = base.find(stripped)
            if is_inside_comment(idx, spans):
                continue
            line_start = base.rfind("\n", 0, idx) + 1
            if not _anchor_line_is_safe(base, line_start):
                continue
            if _anchor_is_in_expression_context(base, idx, path):
                continue
            anchor_indent = base[line_start:idx]
            line_end = base.find("\n", idx)
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1
            reindented = _reindent_body(
                anchor_indent, body.lstrip("\n").rstrip())
            insertion = reindented.rstrip() + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

    return None
