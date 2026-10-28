"""
patch_scope — scope boundary detection and scoped-replace.

See the pre-fix module docstring for the base description of what a
scope is (function/class/const-assignment/<details>) and the
invariants around partial-splice balance, scope-signature integrity,
and full-replace fail-closed.

MULTI-SCOPE BODIES — REFINED
=============================
`_apply_scoped` splits multi-scope bodies via
`_extract_top_level_scopes` and applies each scope separately, BUT
only when *every* sub-scope exists in base.  Two cases:

  ALL sub-scopes exist (pg_pixels.py)
    The body declares two functions that both already live in the
    base.  Single-scope application would embed the second function
    inside the first's span, while the base's original second copy
    survived below — producing `Identifier already declared` at
    load time.  Splitting into two scoped replaces fixes this.

  SOME sub-scopes are NEW functions (pg_panel_bindings.py)
    The body declares `_inpaintAction` (exists) plus two helper
    functions that don't exist in base.  Attempting to scoped-replace
    the new helpers fails (`_scope_in_base` returns False).  Splitting
    propagates that failure and the whole pair is dropped.  The old
    single-scope behavior — replace the first scope's span with the
    whole new_body — correctly *inserts* the new helpers alongside
    the replacement, which is what the AI intended.

PLACEHOLDER MERGE (2026-10-28)
==============================
An AI block often replaces a function body with a *template* whose
docstring contains a placeholder (`... (docstring unchanged) ...`).
Applying the template verbatim replaces the real docstring with the
marker string — the comfyui_krita.py corruption.

`_try_merge_placeholder_in_scope` is the fix: it locates the base
scope, extracts its Python docstring, and merges it into the new
body's placeholder (or prepends it if the new body has no docstring
at all).  Called from `patch_dispatch._try_deterministic_patch_inner`
before any other heuristic.

Handles only Python docstrings (FunctionDef/AsyncFunctionDef/ClassDef
with a leading string constant).  JS comments, `<details>` inner
text, and non-docstring placeholders fall through to the
dispatcher's fail-closed refusal, which is the safe outcome.
"""

import re
import ast

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
#
# See module docstring, PLACEHOLDER MERGE section.


def _find_python_docstring_span(body: str):
    """Return (start_char, end_char) of the first docstring in the
    first FunctionDef/AsyncFunctionDef/ClassDef of `body`, or None.

    Character offsets are into `body`.  `end_char` is exclusive —
    it points past the closing triple quote.

    A "docstring" here means: the first statement of the function
    body is a bare string literal (an `ast.Expr` wrapping an
    `ast.Constant` of type `str`).  This is Python's own definition;
    a string literal as a non-first statement is not a docstring, and
    is not handled.
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
    """Return the first function/class's docstring *content* (the
    string value without surrounding quotes), or None if there is
    no such function or it has no docstring.
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

    Returns `new_body` unchanged when:
      · the new body's docstring contains no placeholder, or
      · the new body's first function has no docstring at all, or
      · the base scope's first function has no docstring.

    When the merge fires, the base docstring's *content* (the string
    value, without surrounding quotes) replaces the marker inside
    the new body's docstring.  The new body's own docstring
    delimiters stay, and any trailing text after the marker (e.g. an
    appended paragraph) is preserved.

    The base content is used verbatim, without re-indent.  A
    docstring's first line typically has no leading whitespace
    (it sits immediately after the opening quote), while its
    continuation lines are indented to match the enclosing scope.
    That layout is exactly what a merged docstring wants, so the
    `ast.get_docstring` value is inserted as-is after trimming its
    surrounding blank lines.  Re-indenting would push the first
    line in by four spaces for no benefit.

    Example layout:

        base docstring body:
            Decode a PNG and write it as a new layer.
            <blank>
                The PNG comes straight from ComfyUI.
                QImage decodes it to Format_ARGB32.

        new body docstring:
            ... (docstring unchanged) ...
            <blank>
                `rotation` is degrees, positive = clockwise.

        merged docstring body:
            Decode a PNG and write it as a new layer.
            <blank>
                The PNG comes straight from ComfyUI.
                QImage decodes it to Format_ARGB32.
            <blank>
                `rotation` is degrees, positive = clockwise.
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

    Returns the merged body, or None if no merge is possible.

    Uses `_SCOPE_PATTERNS` directly (matching `_apply_scoped_single`)
    rather than `_find_scope_in_base`.  `_find_scope_in_base` is
    brace-aware — it requires a `{` in the matched span — so it
    returns None for every Python scope.  That made the merge path
    unreachable for the Python docstring case it exists to handle.
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
#  Scoped replace
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


def _apply_scoped_single(base, scope_name, new_body):
    """Apply a single-scope replacement (the original `_apply_scoped`
    body, renamed).
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
            return base[:indent_pos] + reindented + base[m_end:]

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
        if not _scope_signature_ok(reindented):
            return None
        return base[:indent_pos] + reindented + base[m_end:]
    return None


def _apply_scoped(base, scope_name, new_body):
    """Apply a scoped replace, handling multi-scope bodies correctly.
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
                    current, sub_name, sub_body)
                if result is None:
                    return None
                current = result
            return current

    return _apply_scoped_single(base, scope_name, new_body)
