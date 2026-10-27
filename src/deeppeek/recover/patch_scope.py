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
"""

import re

from .prose import (
    _extract_scope_from_body,
    _SCOPE_FROM_BODY_PATTERNS,
    _string_spans,
    _inside_any,
)
from .patch_text import (
    _has_ellipsis_marker, _split_at_ellipsis,
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
    """True iff `name` exists in `base` as a scope of *any* kind.

    Used by `_apply_scoped`'s multi-scope gate: to decide whether the
    split-into-individual-replaces path is safe, we need to know
    whether every sub-scope of the body has a corresponding span in
    base.  `_scope_in_base` requires an explicit `scope_kind`, which
    we don't carry out of `_extract_top_level_scopes`, so this helper
    tries all three.
    """
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
    body, renamed)."""
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

    Two modes:

    ALL sub-scopes exist in base (e.g. pg_pixels.py)
      Split into individual scoped replaces, one per sub-scope.  This
      is required when the body declares two functions that both
      already live in the base — single-scope application would
      embed the second function inside the first's span while the
      base's original copy survived below (duplicate declaration).

    SOME sub-scopes are new functions (e.g. pg_panel_bindings.py)
      Fall back to the original single-scope behavior: replace the
      first scope's span with the ENTIRE new_body.  This correctly
      *inserts* the new helper functions alongside the replacement.
      Splitting would fail because `_scope_in_base` returns False
      for the new helpers, and the pair would be dropped — leaving
      the first scope's replacement un-applied even though the
      user wanted it.

    The distinction is critical: pg_pixels.py must split (dup bug);
    pg_panel_bindings.py must NOT split (would drop the helper
    additions).
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
        # Fall through: some sub-scopes are new functions.  Use the
        # single-scope behavior, which inserts the whole new_body at
        # the first scope's span.  New helpers land correctly.

    return _apply_scoped_single(base, scope_name, new_body)
