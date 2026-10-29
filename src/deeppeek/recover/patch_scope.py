"""
patch_scope — scope boundary detection and scoped-replace.

See the pre-fix module docstring for the base description of what a
scope is (function/class/const-assignment/<details>) and the
invariants around partial-splice balance, scope-signature integrity,
and full-replace fail-closed.

AST-SNAPPED BOUNDARIES (2026-10-29)
===================================
`_apply_scoped_single` splices via `patch_bounds.splice_region`
instead of the direct `base[:indent_pos] + reindented + base[m_end:]`.
The splice candidate set is:

  1. The unmodified `[start_line, end_line)` computed from the scope
     match.
  2. AST-snapped boundaries — expanded outward to the enclosing
     top-level statements (`ast` for `.py`, tree-sitter for `.js`/
     `.ts`/etc. and for the dominant embedded JS string in a `.py`).
     This is what catches off-by-one endings: a scope-match that stops
     one line short of the closing `}` leaves the old brace as an
     orphan `}`, and the AST snap expands `end` up to the enclosing
     node's end.
  3. A ±3-line sweep around the original boundaries as fallback when
     the AST snap can't fire (tree-sitter missing, empty path, or the
     scope is HTML `<details>` where the JS parser can't help).

Each candidate goes through `patch_bounds.splice_region`, which
validates structurally, and then through `_verify_scope_isolation`
(see below), which rejects candidates that silently damaged a
sibling scope.  The first candidate that passes both wins.  When
none does, `_apply_scoped_single` returns None and fail-closed kicks
in (the record lands in `low_trust/`).

SCOPE-ISOLATION VERIFICATION (2026-10-29)
=========================================
A "scope replace" must replace exactly one scope. Siblings must be
byte-identical before and after. The motivating failure — the
2026-10-29 `pg_panel_bindings.py` run — was a candidate whose
splice boundary swallowed an adjacent function:

    function _pickMaskAction() {           // ← base has these 3 lines
      input.addEventListener("change", onMask);
    }
    function _clearImageAction() {         // ← the scope being replaced
      ...
    }

Applying the replacement with a boundary that cut into
`_pickMaskAction` produced a file where `_clearImageAction` was
replaced correctly but `_pickMaskAction`'s `addEventListener` line
had disappeared.  Both are top-level declarations; nothing in
`splice_region`'s structural check noticed.

`_verify_scope_isolation(base, result, scope_name, path)` is called
on every candidate that passes the structural gate. It compares
*declarations by name*:

  · every base declaration except `scope_name` must appear
    byte-identical in `result`;
  · additional declarations in `result` are allowed (a scope
    replace may add helper functions next to the scope it
    replaces);
  · comments, docstrings, decorators, and any preamble attached
    to `scope_name` may change (they belong to the replaced scope).

Comparison is name-indexed, not position-indexed — a candidate that
swaps two sibling functions' order would fail position-based
comparison but is not a semantic violation, and a candidate that
leaves comments untouched but rewrites one sibling's body would
pass position-based comparison but IS a semantic violation.
Name-indexing gets both right.

Language coverage:

  · `.py`    — `ast.parse`, top-level FunctionDef / AsyncFunctionDef
              / ClassDef.
  · `.js`    — tree-sitter-javascript, top-level function_declaration
              / class_declaration / lexical_declaration / variable_
              declaration / export_statement.
  · `.html`  — tree-sitter-html, top-level `<details>` elements,
              keyed by their `<summary>` text.
  · embedded — JS/HTML inside a `.py` triple-quoted string is
              checked by recursing into the string.

When the language's parser is unavailable (tree-sitter not
installed, or `ast.parse` raises on content that `splice_region`
already validated for structural balance), the isolation check fails
open. The dispatcher's structural gate remains the outer safety
net; isolation exists to catch the class of silent sibling damage
that structural balance cannot detect.

PATH THREADING (2026-10-29)
===========================
`_apply_scoped` and `_apply_scoped_single` take an optional `path`
argument so the AST snap can pick the right grammar, the isolation
check can pick the right parser, and `splice_region` can dispatch to
the right structural validator.  Callers
(`patch_dispatch._try_deterministic_patch_inner`,
`patch_pairs._apply_pairs`) pass the record's path through.

EXCLUSIVE END-LINE (2026-10-30)
===============================
`end_line` is the position *one past* the scope's last line.  The
pre-fix computation was `base.count("\n", 0, m_end)`, which yields the
index of the line containing `base[m_end - 1]` — i.e. the scope's
last line itself, not one past it.  Because `splice_region` replaces
`base_lines[start:end]`, that `end` preserved the scope's last line
into the new body.  Two failure shapes resulted:

  • Whenever the new body re-stated the same trailing line (e.g. a
    method that still ended with `return {'success': True}`), the
    stale line was identical to the new one and the damage was
    invisible.

  • Whenever the new body's last line differed from the old body's
    last line, the old line survived as an *unreachable statement*
    after the new `return`.  `ast.parse` accepts unreachable code, so
    `validate_content` reported `ok` and the file shipped corrupted.

  Concrete instance: `comfyui_krita.py`'s `_encode_alpha8_png` gained
  `return _encode_grayscale_png(bytes(alpha), w, h)` from the OLD
  body, which references the removed local `alpha`.  The file parses
  fine and would raise `NameError` on the first call.

  The fix is the `+ 1`.

Prior docstring content (unchanged):
- MULTI-SCOPE BODIES — REFINED
- PLACEHOLDER MERGE (2026-10-28)
- ALREADY-PRESENT SCOPED-REPLACE (2026-10-28f)
"""

import re
import ast
from pathlib import Path

from .prose import (
    _extract_scope_from_body,
    _SCOPE_FROM_BODY_PATTERNS,
    _string_spans,
    _inside_any,
)
from .patch_text import (
    _has_ellipsis_marker, _split_at_ellipsis,
    _has_placeholder_marker,
    _find_matching_brace, _reindent_body, _insert_lines_in_inner,
    _body_starts_with_comment, _find_leading_comment_start,
    _first_identifier,
    _content_present_in_base,
)


# ===========================================================================
#  Scope patterns
# ===========================================================================

_SCOPE_PATTERNS = [
    lambda name: re.compile(
        r"<details\b[^>]*>\s*<summary>\s*" + re.escape(name) +
        r"\s*</summary>.*?</details>",
        re.DOTALL | re.IGNORECASE),
    lambda name: re.compile(
        r"(?:^|\n)(?:def|class)\s+" + re.escape(name) +
        r"\b.*?(?=\n(?:def|class|@)|\Z)",
        re.DOTALL),
    lambda name: re.compile(
        r"(?:^|\n)(?:export\s+)?(?:async\s+)?"
        r"(?:function|class)\s+" + re.escape(name) + r"\b.*?"
        r"(?:\n\}|\Z)",
        re.DOTALL),
    lambda name: re.compile(
        r"(?:^|\n)(?:export\s+)?(?:const|let|var)\s+" + re.escape(name) +
        r"\s*=\s*\{.*?\n\};?",
        re.DOTALL),
    lambda name: re.compile(
        r"(?:^|\n)[ \t]*" + re.escape(name) + r"\s*=\s*\{.*?\n[ \t]*\};?",
        re.DOTALL),
]


# ===========================================================================
#  Scope-declaration signature check
# ===========================================================================

_SCOPE_DECL_RE_JS = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)"
    r"\s+[A-Za-z_$][\w$]*"
    r"(?:\s+extends\s+[A-Za-z_$][\w$.]*)?"
    r"\s*[\(=:{]",
    re.MULTILINE,
)

_PY_SCOPE_DECL_RE = re.compile(
    r"^(?:async\s+)?(?:def|class)\s+[A-Za-z_]\w*",
    re.MULTILINE,
)

_DETAILS_DECL_RE = re.compile(
    r"^<details\b",
    re.IGNORECASE,
)


def _is_scope_decl_line(line):
    if not line:
        return False
    return bool(
        _SCOPE_DECL_RE_JS.match(line)
        or _PY_SCOPE_DECL_RE.match(line)
        or _DETAILS_DECL_RE.match(line)
    )


def _first_meaningful_line(text):
    if not text:
        return None
    in_block_comment = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if in_block_comment:
            if "*/" not in stripped:
                continue
            after = stripped.split("*/", 1)[1].strip()
            in_block_comment = False
            if not after:
                continue
            stripped = after
        if stripped.startswith("/*"):
            if "*/" in stripped:
                after = stripped.split("*/", 1)[1].strip()
                if not after:
                    continue
                stripped = after
            else:
                in_block_comment = True
                continue
        if stripped.startswith(("//", "#", "@")):
            continue
        return stripped
    return None


def _scope_signature_ok(scope_text):
    first = _first_meaningful_line(scope_text)
    if first is None:
        return False
    return _is_scope_decl_line(first)


# ===========================================================================
#  Scope detection
# ===========================================================================

def _scope_in_base(base, scope_name, scope_kind):
    if not base or not scope_name:
        return False
    if scope_kind == "details":
        return bool(re.search(
            r"<details\b[^>]*>\s*<summary>\s*"
            + re.escape(scope_name) + r"\s*</summary>",
            base, re.IGNORECASE | re.DOTALL))
    esc = re.escape(scope_name)
    if scope_kind == "python":
        return bool(re.search(
            rf"^(?:async\s+)?(?:def|class)\s+{esc}\b",
            base, re.MULTILINE))
    if re.search(rf"^(?:export\s+)?(?:async\s+)?"
                 rf"(?:function|class)\s+{esc}\b", base, re.MULTILINE):
        return True
    if re.search(rf"^(?:export\s+)?(?:const|let|var)\s+{esc}\s*=\s*\{{",
                 base, re.MULTILINE):
        return True
    return False


def _scope_exists_any_kind(base, name):
    return (
        _scope_in_base(base, name, "javascript")
        or _scope_in_base(base, name, "python")
        or _scope_in_base(base, name, "details")
    )


def _find_scope_in_base(base, scope_name):
    for make_pattern in _SCOPE_PATTERNS:
        try:
            pattern = make_pattern(scope_name)
        except re.error:
            continue
        m = pattern.search(base)
        if not m:
            continue
        matched = base[m.start():m.end()]
        if matched.lstrip().startswith("<details"):
            return None
        brace_open = base.find("{", m.start(), m.end())
        if brace_open == -1:
            continue
        brace_close = _find_matching_brace(base, brace_open)
        if brace_close == -1:
            continue
        return (m.start(), brace_open + 1, brace_close, brace_close + 1)
    return None


def _matches_scope_declaration(line, scope_name):
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    esc = re.escape(scope_name)
    if re.match(r'^(?:async\s+)?def\s+' + esc + r'\s*\(', stripped):
        return True
    if re.match(r'^class\s+' + esc + r'\b', stripped):
        return True
    if re.match(r'^(?:export\s+)?(?:async\s+)?function\s+' + esc +
                r'\s*\(', stripped):
        return True
    if re.match(r'^(?:export\s+)?(?:const|let|var)\s+' + esc +
                r'\s*=', stripped):
        return True
    if re.match(r'^(?:export\s+)?(?:async\s+)?class\s+' + esc +
                r'\b', stripped):
        return True
    if re.match(r'^' + esc + r'\s*=', stripped):
        return True
    return False


_SCOPE_CLOSE_RE = re.compile(r'^\s*(?:\}|\};|\};?\s*\}\s*;?|</details>)\s*$',
                             re.IGNORECASE)


def _looks_like_scope_close(line):
    return bool(_SCOPE_CLOSE_RE.match(line))


# ===========================================================================
#  Placeholder merge  (2026-10-28)
# ===========================================================================

def _find_python_docstring_span(body: str):
    """Return (start_char, end_char) of the first docstring in the
    first FunctionDef/AsyncFunctionDef/ClassDef of `body`, or None.

    Character offsets are into `body`.  `end_char` is exclusive —
    it points past the closing triple quote.
    """
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return None
    if not tree.body:
        return None
    func = tree.body[0]
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
        return None
    if not func.body:
        return None
    first_stmt = func.body[0]
    if not (isinstance(first_stmt, ast.Expr)
            and isinstance(first_stmt.value, ast.Constant)
            and isinstance(first_stmt.value.value, str)):
        return None
    try:
        start_line = first_stmt.lineno - 1
        start_col = first_stmt.col_offset
        end_line = first_stmt.end_lineno - 1
        end_col = first_stmt.end_col_offset
    except AttributeError:
        return None

    lines = body.split("\n")
    line_starts = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln) + 1)
    if start_line >= len(line_starts) or end_line >= len(line_starts):
        return None
    start_char = line_starts[start_line] + start_col
    end_char = line_starts[end_line] + end_col
    return (start_char, end_char)


def _extract_python_docstring(body: str):
    """Return the first function/class's docstring *content*."""
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return None
    if not tree.body:
        return None
    func = tree.body[0]
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
        return None
    return ast.get_docstring(func, clean=False)


_PLACEHOLDER_MARKER_RE = re.compile(
    r"\.\.\.\s*\(\s*(?:the\s+)?[^)]*?\b"
    r"(?:unchanged|existing|omitted|same|as\s+before|as-is|as\s-is|"
    r"kept|preserved|unchanged\s+except|below|above|rest)"
    r"\b[^)]*\)\s*\.\.\.",
    re.IGNORECASE,
)


def _merge_docstring_with_placeholder(base_scope: str,
                                      new_body: str) -> str:
    """If `new_body`'s function docstring begins with a placeholder
    marker, replace the marker with `base_scope`'s docstring content.
    """
    new_doc_span = _find_python_docstring_span(new_body)
    if new_doc_span is None:
        return new_body
    start, end = new_doc_span
    new_doc_text = new_body[start:end]
    if not _PLACEHOLDER_MARKER_RE.search(new_doc_text):
        return new_body

    base_doc_content = _extract_python_docstring(base_scope)
    if base_doc_content is None:
        return new_body

    base_clean = base_doc_content.strip()
    merged_doc = _PLACEHOLDER_MARKER_RE.sub(
        lambda _m: base_clean, new_doc_text, count=1)
    return new_body[:start] + merged_doc + new_body[end:]


def _try_merge_placeholder_in_scope(base: str,
                                    scope_name: str,
                                    new_body: str):
    """Attempt to consume a placeholder marker in `new_body` by
    merging the base scope's content.
    """
    if not scope_name or not new_body:
        return None
    for make_pattern in _SCOPE_PATTERNS:
        try:
            pattern = make_pattern(scope_name)
        except re.error:
            continue
        m = pattern.search(base)
        if not m:
            continue
        base_scope = base[m.start():m.end()]
        if not base_scope:
            continue
        merged = _merge_docstring_with_placeholder(base_scope, new_body)
        if merged != new_body:
            return merged
    return None


# ===========================================================================
#  Multi-scope extraction
# ===========================================================================

def _extract_details_span(body, start_pos):
    close = body.find("</details>", start_pos)
    if close == -1:
        return None
    return body[start_pos:close + len("</details>")]


def _extract_js_span(body, start_pos):
    open_brace = body.find("{", start_pos)
    if open_brace == -1:
        return None
    close_brace = _find_matching_brace(body, open_brace, is_python=False)
    if close_brace == -1:
        return None
    return body[start_pos:close_brace + 1]


def _extract_python_span(body, start_pos):
    line_start = body.rfind("\n", 0, start_pos) + 1
    indent = start_pos - line_start
    line_end = body.find("\n", start_pos)
    if line_end == -1:
        return body[start_pos:]
    pos = line_end + 1
    while pos < len(body):
        next_end = body.find("\n", pos)
        if next_end == -1:
            next_end = len(body)
        line = body[pos:next_end]
        if line.strip():
            line_indent = len(line) - len(line.lstrip())
            if line_indent <= indent and not line.strip().startswith("#"):
                return body[start_pos:pos].rstrip()
        pos = next_end + 1
    return body[start_pos:].rstrip()


def _extract_top_level_scopes(body):
    """Return [(name, scope_body_text)] for each top-level scope
    declared in `body`, or [] when the body has zero or one scope."""
    if not body:
        return []
    spans = _string_spans(body)
    candidates = []
    for pattern, kind in _SCOPE_FROM_BODY_PATTERNS:
        for m in pattern.finditer(body):
            if _inside_any(m.start(), spans):
                continue
            if m.start() > 0 and body[m.start() - 1] != "\n":
                if not m.group(0).startswith("<details"):
                    continue
            candidates.append((m.start(), m.group(1).strip(), kind))

    if len(candidates) < 2:
        return []

    seen_starts = set()
    unique = []
    for start, name, kind in sorted(candidates):
        if start in seen_starts:
            continue
        seen_starts.add(start)
        unique.append((start, name, kind))

    if len(unique) < 2:
        return []

    results = []
    for i, (start, name, kind) in enumerate(unique):
        if kind == "details":
            scope_body = _extract_details_span(body, start)
        elif kind == "python":
            scope_body = _extract_python_span(body, start)
        else:
            scope_body = _extract_js_span(body, start)
        if not scope_body:
            return []
        results.append((name, scope_body))

    return results


# ===========================================================================
#  Prefix-modification helpers
# ===========================================================================

def _is_comment_only(lines):
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if not (s.startswith("//") or s.startswith("#")
                or s.startswith("/*") or s.endswith("*/")
                or s.startswith("*")):
            return False
    return True


def _nearest_ident_line(base_stripped, base_pos, target_ident,
                        search_ahead=3):
    checked = 0
    for j in range(base_pos, len(base_stripped)):
        line = base_stripped[j]
        if not line:
            continue
        checked += 1
        if checked > search_ahead:
            return None
        if _first_identifier(line) == target_ident:
            return j
    return None


def _try_prefix_modification(inner_prefix, base_scope_inner):
    if not inner_prefix:
        return None

    base_lines = base_scope_inner.split("\n")
    base_stripped = [ln.strip() for ln in base_lines]

    result = []
    base_pos = 0
    i = 0
    saw_new_run = False

    while i < len(inner_prefix):
        ln = inner_prefix[i]
        stripped = ln.strip()
        if not stripped:
            if base_pos < len(base_stripped) and not base_stripped[base_pos]:
                result.append(base_lines[base_pos])
                base_pos += 1
            else:
                result.append(ln)
            i += 1
            continue

        try:
            match_idx = base_stripped.index(stripped, base_pos)
        except ValueError:
            match_idx = -1

        if match_idx >= 0:
            result.extend(base_lines[base_pos:match_idx + 1])
            base_pos = match_idx + 1
            i += 1
            continue

        run_lines = []
        while i < len(inner_prefix):
            curr = inner_prefix[i]
            curr_stripped = curr.strip()
            if curr_stripped and curr_stripped in base_stripped:
                break
            run_lines.append(curr)
            i += 1

        if not run_lines:
            return None

        first_run_stripped = run_lines[0].strip()
        target_ident = _first_identifier(first_run_stripped)
        if not target_ident:
            return None
        replace_at = _nearest_ident_line(
            base_stripped, base_pos, target_ident)
        if replace_at is None:
            return None

        if len(run_lines) < 2:
            return None

        result.extend(base_lines[base_pos:replace_at])
        result.extend(run_lines)
        base_pos = replace_at + 1
        saw_new_run = True

    result.extend(base_lines[base_pos:])

    if not saw_new_run:
        return None

    return "\n".join(result)


# ===========================================================================
#  Ellipsis splice
# ===========================================================================

def _try_ellipsis_splice(base, body, context_before=""):
    if not _has_ellipsis_marker(body):
        return None
    scope_name, _kind = _extract_scope_from_body(body)
    if not scope_name:
        return None
    scope_info = _find_scope_in_base(base, scope_name)
    if scope_info is None:
        return None
    decl_start, body_open, body_close, scope_end = scope_info

    prefix_lines, suffix_lines = _split_at_ellipsis(body)
    if not prefix_lines:
        return None
    if not _matches_scope_declaration(prefix_lines[0], scope_name):
        return None

    inner_prefix = prefix_lines[1:]
    if suffix_lines and _looks_like_scope_close(suffix_lines[-1]):
        inner_suffix = suffix_lines[:-1]
    else:
        inner_suffix = suffix_lines

    base_scope_inner = base[body_open:body_close]

    modified = _try_prefix_modification(inner_prefix, base_scope_inner)
    if modified is not None:
        if modified == base_scope_inner:
            return base
        return base[:body_open] + modified + base[body_close:]

    if _is_comment_only(inner_suffix):
        inner_suffix = []

    base_stripped_set = {
        ln.strip() for ln in base_scope_inner.splitlines() if ln.strip()
    }

    new_prefix = [ln for ln in inner_prefix
                  if ln.strip() and ln.strip() not in base_stripped_set]
    new_suffix = [ln for ln in inner_suffix
                  if ln.strip() and ln.strip() not in base_stripped_set]

    if not new_prefix and not new_suffix:
        return base

    prefix_anchor = None
    for ln in inner_prefix:
        if ln.strip() and ln.strip() in base_stripped_set:
            prefix_anchor = ln.strip()
            break

    suffix_anchor = None
    for ln in reversed(inner_suffix):
        if ln.strip() and ln.strip() in base_stripped_set:
            suffix_anchor = ln.strip()
            break

    result_inner = base_scope_inner
    if new_prefix:
        result_inner = _insert_lines_in_inner(
            result_inner, new_prefix,
            before_anchor=prefix_anchor, default_position="top")
    if new_suffix:
        result_inner = _insert_lines_in_inner(
            result_inner, new_suffix,
            after_anchor=suffix_anchor, default_position="bottom")

    return base[:body_open] + result_inner + base[body_close:]


# ===========================================================================
#  Scoped-replace
# ===========================================================================

def _new_body_is_complete_for_scope(base_scope_text, new_body):
    base_stripped = base_scope_text.rstrip()
    new_stripped = new_body.rstrip()
    if not new_stripped:
        return False
    if base_stripped.endswith("</details>"):
        return new_stripped.endswith("</details>")
    if base_stripped.endswith("}"):
        return (new_stripped.endswith("}")
                or new_stripped.endswith("};"))
    return True


def _try_scoped_partial_splice(base_scope_text, new_body):
    import difflib
    from .validate_output import _js_balance

    base_lines = base_scope_text.split("\n")
    new_lines = new_body.split("\n")
    sm = difflib.SequenceMatcher(None, base_lines, new_lines)
    result = list(base_lines)
    added_lines = 0
    for tag, i1, i2, j1, j2 in reversed(sm.get_opcodes()):
        if tag == 'equal':
            continue
        if tag == 'insert':
            added_lines += (j2 - j1)
            result[i1:i1] = new_lines[j1:j2]
        elif tag == 'replace':
            if (i2 - i1) <= (j2 - j1) * 2:
                added_lines += (j2 - j1)
                result[i1:i2] = new_lines[j1:j2]
    if added_lines == 0:
        return None
    result_text = "\n".join(result)
    if result_text == base_scope_text:
        return None
    if _js_balance(result_text) != _js_balance(base_scope_text):
        return None
    return result_text


# ===========================================================================
#  Scope-isolation verification  (2026-10-29)
# ===========================================================================
#
# See the module docstring, SCOPE-ISOLATION VERIFICATION.  Short
# version: a scope-replace on X may change X and anything attached to
# X (comments, docstrings, decorators, leading preamble).  It must
# NOT change any *other* top-level declaration.  Additional
# declarations may be added — a scope-replace that also introduces
# helpers next to the scope it replaces is a legitimate shape.
#
# The check is name-indexed on purpose.  Reordering two sibling
# functions is legal; changing one sibling's body while leaving its
# name in place is not.  Position-based comparison gets both wrong.

_JS_DECL_NODE_TYPES = frozenset({
    "function_declaration",
    "generator_function_declaration",
    "class_declaration",
    "lexical_declaration",
    "variable_declaration",
    "export_statement",
})


def _node_text(node):
    t = node.text
    if isinstance(t, bytes):
        return t.decode("utf-8", "replace")
    return t or ""


def _verify_scope_isolation(base, result, scope_name, path=""):
    """Reject a scope-replace that also modified a sibling scope.

    Compares *declarations* by name — functions, classes, top-level
    bindings — not raw position, not comments, not preamble. The
    pg_panel_bindings.py motivating case (`_pickMaskAction` truncated
    alongside the intended `_clearImageAction` change) is caught by
    "base declaration changed but its name isn't `scope_name`".

    Rules:
      · Every base declaration except `scope_name` must appear
        unchanged in `result`.
      · Additional declarations in `result` are allowed (a
        scope-replace may add helper functions next to the scope
        it replaces).
      · Comments, docstrings, decorators, and any preamble
        attached to `scope_name` may change (they belong to the
        replaced scope).
      · Fails open when the content cannot be parsed (tree-sitter
        missing, syntax error in an unrelated region). The
        dispatcher's structural gate is the outer safety net; this
        check exists to prevent silent sibling damage in the cases
        where parsing succeeds.

    Returns True iff isolation holds.
    """
    if not base or not result:
        return False

    ext = Path(path).suffix.lower() if path else ""

    if ext == ".py":
        if not _py_decls_isolated(base, result, scope_name):
            return False
        return _embedded_html_isolated(base, result, scope_name)
    if ext in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
        return _js_decls_isolated(base, result, scope_name)
    if ext == ".html":
        return _html_details_isolated(base, result, scope_name)
    return True


def _py_decls_isolated(base, result, scope_name):
    try:
        base_tree = ast.parse(base)
        result_tree = ast.parse(result)
    except SyntaxError:
        return True
    base_lines = base.splitlines()
    result_lines = result.splitlines()

    def _decls(tree, lines):
        out = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            text = "\n".join(
                lines[node.lineno - 1:node.end_lineno])
            out[node.name] = text
        return out

    base_decls = _decls(base_tree, base_lines)
    result_decls = _decls(result_tree, result_lines)
    for name, text in base_decls.items():
        if name == scope_name:
            continue
        if name not in result_decls:
            return False
        if result_decls[name] != text:
            return False
    return True


def _js_decls_isolated(base, result, scope_name):
    try:
        from .ast_anchor import _get_parser
    except ImportError:
        return True
    parser = _get_parser("javascript")
    if parser is None:
        return True

    def _decls(content):
        try:
            tree = parser.parse(content.encode("utf-8"))
        except Exception:
            return None
        if tree.root_node.has_error:
            return None
        lines = content.splitlines()
        out = {}
        for child in tree.root_node.children:
            name = _js_decl_name(child)
            if name is None:
                continue
            text = "\n".join(
                lines[child.start_point[0]:child.end_point[0] + 1])
            out[name] = text
        return out

    base_decls = _decls(base)
    result_decls = _decls(result)
    if base_decls is None or result_decls is None:
        return True
    for name, text in base_decls.items():
        if name == scope_name:
            continue
        if name not in result_decls:
            return False
        if result_decls[name] != text:
            return False
    return True


def _js_decl_name(node):
    if node.type == "export_statement":
        for child in node.children:
            n = _js_decl_name(child)
            if n:
                return n
        return None
    if node.type in ("function_declaration",
                     "generator_function_declaration",
                     "class_declaration"):
        for child in node.children:
            if child.type == "identifier":
                return _node_text(child)
        return None
    if node.type in ("lexical_declaration", "variable_declaration"):
        for child in node.children:
            if child.type == "variable_declarator":
                for c in child.children:
                    if c.type == "identifier":
                        return _node_text(c)
        return None
    return None


def _html_details_isolated(base, result, scope_name):
    base_map = _html_details_map(base)
    result_map = _html_details_map(result)
    if base_map is None or result_map is None:
        return True
    for summary, text in base_map.items():
        if summary == scope_name:
            continue
        if summary not in result_map:
            return False
        if result_map[summary] != text:
            return False
    return True


def _html_details_map(content):
    try:
        import tree_sitter_html  # type: ignore
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        lang = Language(tree_sitter_html.language())
        parser = Parser(lang)
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return None
    if tree.root_node.has_error:
        return None
    lines = content.splitlines()
    out = {}

    def _walk(node):
        if node.type == "element":
            tag = _html_tag_name(node)
            if tag == "details":
                summary = _html_summary_of(node, content)
                if summary:
                    text = "\n".join(
                        lines[node.start_point[0]:node.end_point[0] + 1])
                    out[summary] = text
                return
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return out


def _html_tag_name(node):
    for child in node.children:
        if child.type in ("start_tag", "self_closing_tag"):
            for c in child.children:
                if c.type == "tag_name":
                    return _node_text(c).lower()
    return None


def _html_summary_of(details_node, content):
    for child in details_node.children:
        if child.type == "element" and _html_tag_name(child) == "summary":
            text = "\n".join(
                content.splitlines()[
                    child.start_point[0]:child.end_point[0] + 1])
            m = re.search(r"<summary[^>]*>(.*?)</summary>",
                          text, re.DOTALL | re.IGNORECASE)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _embedded_html_isolated(base, result, scope_name):
    pat = re.compile(
        r'(?:[rbufRBUF]{0,2})("""|\'\'\')(.*?)\1',
        re.DOTALL,
    )
    base_strs = [m.group(2) for m in pat.finditer(base)
                 if len(m.group(2)) >= 200]
    result_strs = [m.group(2) for m in pat.finditer(result)
                   if len(m.group(2)) >= 200]
    for bs, rs in zip(base_strs, result_strs):
        if not _html_details_isolated(bs, rs, scope_name):
            return False
    return True


# ===========================================================================
#  Scoped-replace — application
# ===========================================================================

def _apply_scoped_single(base, scope_name, new_body, path=""):
    """Apply a single-scope replacement.

    AST-snapped boundaries + structural validation + scope-isolation
    verification, per the module docstring.  Returns None when no
    candidate splice produced valid, isolated content.
    """
    if not scope_name or not new_body.strip():
        return None
    if _has_ellipsis_marker(new_body):
        return _try_ellipsis_splice(base, new_body)
    for make_pattern in _SCOPE_PATTERNS:
        try:
            pattern = make_pattern(scope_name)
        except re.error:
            continue
        m = pattern.search(base)
        if not m:
            continue
        m_start = m.start()
        m_end = m.end()
        base_scope_text = base[m_start:m_end]

        if not _new_body_is_complete_for_scope(
                base_scope_text, new_body):
            spliced = _try_scoped_partial_splice(
                base_scope_text, new_body)
            if spliced is None:
                return None
            if m_start < len(base) and base[m_start] == "\n":
                indent_pos = m_start + 1
                base_indent = ""
            else:
                indent_pos = base.rfind("\n", 0, m_start) + 1
                base_indent = base[indent_pos:m_start]
            reindented = _reindent_body(base_indent, spliced)
            if not _scope_signature_ok(reindented):
                return None
            partial_result = (
                base[:indent_pos] + reindented + base[m_end:])
            if not _verify_scope_isolation(
                    base, partial_result, scope_name, path or ""):
                return None
            return partial_result

        if m_start < len(base) and base[m_start] == "\n":
            indent_pos = m_start + 1
            base_indent = ""
        else:
            indent_pos = base.rfind("\n", 0, m_start) + 1
            base_indent = base[indent_pos:m_start]
        if _body_starts_with_comment(new_body):
            extended_start = _find_leading_comment_start(
                base, indent_pos)
            if extended_start < indent_pos:
                indent_pos = extended_start
        while m_end > m_start and base[m_end - 1] == "\n":
            m_end -= 1
        reindented = _reindent_body(base_indent, new_body)

        # Already-present scoped-replace (2026-10-28f).
        if _content_present_in_base(base_scope_text, reindented,
                                     threshold=0.99):
            return base

        if not _scope_signature_ok(reindented):
            return None

        # AST-snapped splice with structural validation (2026-10-29)
        # and scope-isolation verification (2026-10-29).
        from .patch_bounds import (
            try_boundary_pairs, try_ast_snapped_boundaries,
            splice_region,
        )
        base_lines = base.splitlines()
        start_line = base.count("\n", 0, indent_pos)
        # EXCLUSIVE end-line.  `splice_region` replaces
        # `base_lines[start:end]`, so `end` must be one past the
        # scope's last line.  The pre-2026-10-30 computation —
        # `base.count("\n", 0, m_end)` — returned the line index of
        # `base[m_end - 1]` itself, i.e. the scope's last line.  That
        # left the last line of every scope-replace intact in the new
        # body.  When the new body re-stated the same last line, the
        # damage was invisible; when it did not, the old last line
        # survived as an unreachable statement (which `ast.parse`
        # accepts).  See the module docstring, EXCLUSIVE END-LINE.
        end_line = base.count("\n", 0, m_end) + 1
        body_lines = reindented.split("\n")

        candidates = [(start_line, end_line)]
        snapped = try_ast_snapped_boundaries(
            base, path or "", start_line, end_line, reindented)
        if snapped is not None and snapped != (start_line, end_line):
            candidates.append(snapped)
        candidates.extend(try_boundary_pairs(
            start_line, end_line, len(base_lines)))

        for trial_start, trial_end in candidates:
            # Scope replaces legitimately shrink (function body
            # replaced with something smaller); 30% floor rather
            # than 50%.
            result = splice_region(
                base, trial_start, trial_end, body_lines,
                path or "", min_fraction=0.3)
            if result is None:
                continue
            if not _verify_scope_isolation(
                    base, result, scope_name, path or ""):
                continue
            return result
        return None
    return None


def _apply_scoped(base, scope_name, new_body, path=""):
    """Apply a scoped replace, handling multi-scope bodies correctly.

    `path` is forwarded to `_apply_scoped_single` for AST snapping,
    structural validation, and scope-isolation verification.

    Two multi-scope cases:

      · All sub-scopes exist in base: apply each as its own
        scoped-replace (the pg_pixels.py fix — two adjacent
        functions rewritten in one block, split into two replaces).

      · Some sub-scopes are new (not in base): fall through to the
        single-scope path.  The new helpers land inside the first
        scope's replacement region — the whole new_body is spliced
        into the first scope's span, so the helpers appear at the
        position the AI placed them (the pg_panel_bindings.py fix —
        `_inpaintAction` replaced, `_rotatedMaskToCanvasB64` and
        `_setOriginalFromB64` added next to it).
    """
    if not scope_name or not new_body.strip():
        return None
    if _has_ellipsis_marker(new_body):
        return _try_ellipsis_splice(base, new_body)

    scopes = _extract_top_level_scopes(new_body)
    if len(scopes) >= 2:
        all_exist = all(
            _scope_exists_any_kind(base, name)
            for name, _sub_body in scopes
        )
        if all_exist:
            current = base
            for sub_name, sub_body in scopes:
                result = _apply_scoped_single(
                    current, sub_name, sub_body, path=path)
                if result is None:
                    return None
                current = result
            return current

    return _apply_scoped_single(base, scope_name, new_body, path=path)
