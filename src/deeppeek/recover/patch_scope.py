"""
patch_scope — scope boundary detection and scoped-replace.

A "scope" is a `function foo()`, `def foo():`, `class Foo {`,
`export const foo = {`, `<details><summary>Foo</summary>`, or a
plain assignment `foo.bar = {`.  Every one of these shapes is found
by a pattern in `_SCOPE_PATTERNS`, which the rest of the pipeline
consults to decide "does the body target an existing named scope?".

The scoped-replace heuristic (`_apply_scoped`) is the most
load-bearing path in the whole pipeline: it is what lets a single
conversation block declare "here is the whole new `foo()`" and have
that block replace `foo()` without duplicating anything.

Separately, `_try_ellipsis_splice` handles the "prefix + `...` +
suffix" abbreviation shape.  It lives here rather than in
patch_heuristics because it is *scope-bound* — the splice target is
always a scope, never a bare statement.

The `_scope_in_base` predicate is exported for the dispatch layer's
pre-check (patch_dispatch), which routes scope-declaring bodies to
this module *before* they can fall through to the insert heuristics.

PARTIAL-SPLICE BALANCE VALIDATION
=================================
`_try_scoped_partial_splice` line-diffs a truncated scope body
against the base scope and inserts the new lines.  A body ending
with a *comment* (e.g. `// ... rest unchanged; ...`) instead of a
`...` marker or the actual scope closer is a partial body that will
never carry a balanced brace count on its own.

When the line-diff produces a result whose brace/paren/bracket
balance differs from the base scope's, the resulting content is
unbalanced and will fail-closed at the sanity gate — but the patch
has already been recorded as a failure, dragging the record to LOW
trust for no good reason.  We catch it here and refuse the splice
instead.  This is the pg_pixels.py failure case: block 99 ends with
`// ... rest unchanged; ...` and the splice dropped the base's
closing `}`.

SCOPE SIGNATURE INTEGRITY
=========================
After a scoped replace, the resulting scope text must still START
with a recognisable declaration line.  A scoped replace that produces
`outputRotation body: JSON.stringify({...})` as the new "declaration"
is broken in a way that a brace-balance check cannot detect (the
braces are balanced — it just isn't a declaration).  This is the
pg_comfy_client.py corruption shape.  `_scope_signature_ok` is the
check; `_apply_scoped` returns None when it fails.

FULL-REPLACE FALLTHROUGH MUST FAIL CLOSED
=========================================
When `_try_scoped_partial_splice` returns None, the body is
incomplete AND unspliceable.  A full replacement of the scope with
an incomplete body is *guaranteed* to leave an unbalanced brace
(and, with high probability, to nest the body's trailing comment
into an unclosed block).  `_apply_scoped` therefore returns None
in that case, so the caller records the pair as failed and the
record fail-closes.  The pg_pixels.py corruption shape (a scoped
pair whose body omitted its final `}` — block 99 — was silently
accepted as a full replace, leaving `+1` unbalanced brace).

Imports from patch_text; imports from prose (for
`_extract_scope_from_body`).

No cyclic risk: patch_heuristics does not import this module, only
patch_dispatch (which imports both) and patch_diag (which imports
this for `_find_scope_in_base` and `_SCOPE_PATTERNS`).
"""

import re

from .prose import _extract_scope_from_body
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
    # Plain (possibly dotted) assignment: `_els = {`, `board.x = {`.
    lambda name: re.compile(
        r"(?:^|\n)[ \t]*" + re.escape(name) + r"\s*=\s*\{.*?\n[ \t]*\};?",
        re.DOTALL),
]


# ===========================================================================
#  Scope-declaration signature check
# ===========================================================================
#
# After a scoped replace, the resulting scope must still start with a
# recognisable declaration. A body like
#
#     outputRotation body: JSON.stringify({
#         canvas_b64: canvasB64,
#         ...
#     }),}) {
#
# has balanced braces, but it is not a declaration. This is the exact
# corruption shape produced by an orphan-patch insert inside a scoped
# replace; without this check the file is written as valid-JSON-in-
# signature-parens, which is neither valid JS nor a recognisable
# function.
#
# The check must accept four declaration "families":
#
#   · JavaScript: function / class / const / let / var, optionally
#     with `export` and/or `async`, optionally with `extends`.
#   · Python: def / class, optionally `async`-prefixed.
#   · HTML: <details> scope blocks.
#   · Anything that precedes those — leading comments (`//`, `#`,
#     `/* … */` single- or multi-line) and decorators (`@…`) — is
#     skipped before the first-meaningful-line test.
#
# Anything else fails.  Concretely that rejects the
# pg_comfy_client.py shape (a signature mangled into a bare
# identifier expression) while accepting:
#
#   · test_10's `<details class="section" open>` first line,
#   · test_apply_scoped_consumes_comment_with_blank's
#     `/* New docstring */` leading line,
#   · test_apply_scoped_consumes_decorator's `// new` leading line.

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
    """True iff `line` begins with a scope declaration in one of the
    languages deeppeek handles: JS function/class/const/let/var (with
    optional `export`, `async`, and `extends`), Python def/class (with
    optional `async`), or an HTML `<details>` block.
    """
    if not line:
        return False
    return bool(
        _SCOPE_DECL_RE_JS.match(line)
        or _PY_SCOPE_DECL_RE.match(line)
        or _DETAILS_DECL_RE.match(line)
    )


def _first_meaningful_line(text):
    """Return the first line of `text` that is not blank, not a line
    comment (`//` or `#`), not a `/* ... */` block comment (single-
    or multi-line), and not a decorator (`@...`).

    Returns None if no such line exists.

    This is the SINGLE source of truth for "what counts as leading
    noise" in scope-signature checks.  Consumers (currently only
    `_scope_signature_ok`) must not inline the skipping logic — that
    was the source of the previous divergence between
    `_scope_signature_ok`'s first-line check and the partial/full
    paths in `_apply_scoped`.
    """
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
    """The replaced scope must still start with a recognisable
    declaration line, after skipping leading comments and decorators.

    Accepted shapes:
      - JS: `function`/`class`/`const`/`let`/`var` declarations
        (with optional `export`/`async` prefixes and `extends`
        clauses)
      - Python: `def`/`class`, optionally `async`-prefixed
      - HTML: `<details>` scope blocks

    Rejected: anything else.  Concretely this catches the
    pg_comfy_client.py corruption shape — a body whose first
    identifier is not a scope-decl keyword
    (`outputRotation body: JSON.stringify({...})`).
    """
    first = _first_meaningful_line(scope_text)
    if first is None:
        return False
    return _is_scope_decl_line(first)


# ===========================================================================
#  Scope detection
# ===========================================================================

def _scope_in_base(base, scope_name, scope_kind):
    """True iff `scope_name` exists in `base` as an extractable scope
    of the given kind.

    Conservative: if extraction doesn't find it, we don't claim it
    exists.
    """
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
    # javascript (default)
    if re.search(rf"^(?:export\s+)?(?:async\s+)?"
                 rf"(?:function|class)\s+{esc}\b", base, re.MULTILINE):
        return True
    if re.search(rf"^(?:export\s+)?(?:const|let|var)\s+{esc}\s*=\s*\{{",
                 base, re.MULTILINE):
        return True
    return False


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
#  Prefix-modification helpers (Fix 9)
# ===========================================================================
#
# A body of the form:
#
#     export function foo() {
#       ... context lines ...
#       <new lines replacing an existing line>
#       // ... rest unchanged
#     }
#
# is a PREFIX MODIFICATION: one existing line is replaced by a run of
# new lines, and the rest of the scope body is preserved verbatim.
# Pure-insert semantics would duplicate the existing line (or place
# the new lines at the wrong position).  The motivating shape:
# `computeMaskBBox` in pg_pixels.py replaces one
# `const W = ... , H = ...;` with two separate `const W = ...;` /
# `const H = ...;` declarations, marked with `// ... rest unchanged`.
#
# The semantics below are deliberately narrow:
#   · context lines must appear verbatim in base;
#   · a run of NEW lines replaces exactly ONE base line, matched by
#     the run's first line's first non-keyword identifier within a
#     small window of base positions;
#   · a single-line run is INSERT, not REPLACE — the caller falls
#     back to pure-insert semantics (this is what makes the
#     pg_view_inpaint.py "add an early-out guard" shape work);
#   · if the identifier match fails, prefix-mod returns None and the
#     caller falls back to pure-insert;
#   · comment-only suffix is documentation (dropped).

def _is_comment_only(lines):
    """True iff every non-blank line in `lines` is a comment."""
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
    """Find a base line at position >= base_pos whose first
    non-keyword identifier matches `target_ident`, within
    `search_ahead` non-blank lines.

    Blank lines are skipped without consuming the search budget, so
    a single blank separator between context and the replaced line
    does not eat the window.  Returns the line index, or None.
    """
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
    """Splice `inner_prefix` into `base_scope_inner` under
    prefix-modification semantics.

    `inner_prefix` is a mix of context lines (present verbatim in
    base) and one-or-more runs of NEW lines.  Each run REPLACES one
    corresponding base line rather than being inserted next to it.
    The match is: the run's FIRST line has the same first
    non-keyword identifier as the base line being replaced, located
    within `_nearest_ident_line`'s search window from the current
    base position.

    Fires only when a run has >= 2 lines.  A single-line run is an
    INSERT (a new statement before an existing one), not a
    replacement; the pure-insert path handles that correctly.  The
    fixture 18 shape (pg_view_inpaint.py: `if (board.fullCanvas)
    return;` inserted before `if (!board.imageEl) return;`) is a
    single-line run and must not fire prefix-mod.

    Returns the spliced scope text, or None if the shape doesn't
    fit prefix-mod (caller should fall back to insert semantics).
    """
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
            # Blank line: if the next base line is also blank,
            # consume it from base (preserving base's whitespace).
            # Otherwise emit the inner blank standalone.
            if base_pos < len(base_stripped) and not base_stripped[base_pos]:
                result.append(base_lines[base_pos])
                base_pos += 1
            else:
                result.append(ln)
            i += 1
            continue

        # Context line: present verbatim in base from base_pos onward.
        try:
            match_idx = base_stripped.index(stripped, base_pos)
        except ValueError:
            match_idx = -1

        if match_idx >= 0:
            result.extend(base_lines[base_pos:match_idx + 1])
            base_pos = match_idx + 1
            i += 1
            continue

        # NEW run starting at i.  Collect consecutive lines that
        # are NOT verbatim in base (skipping over blanks that
        # belong to the run itself).
        run_lines = []
        while i < len(inner_prefix):
            curr = inner_prefix[i]
            curr_stripped = curr.strip()
            if curr_stripped and curr_stripped in base_stripped:
                break
            run_lines.append(curr)
            i += 1

        if not run_lines:
            return None  # shouldn't happen; guard

        # Locate the base line this run replaces.  Must match on
        # the FIRST line's identifier and be within a few lines of
        # base_pos.  If no match, this run is a pure insert, not a
        # modification — refuse and let the caller try insert
        # semantics.
        first_run_stripped = run_lines[0].strip()
        target_ident = _first_identifier(first_run_stripped)
        if not target_ident:
            return None
        replace_at = _nearest_ident_line(
            base_stripped, base_pos, target_ident)
        if replace_at is None:
            return None

        # Discriminate prefix-modification from pure-insert.  A
        # run of one new line is an INSERT (a new statement before
        # an existing one); prefix-modification replaces one base
        # line with a MULTI-LINE run.  The pg_view_inpaint.py shape
        # (`if (board.inpaintFullCanvas) return;` inserted before
        # `if (!board.imageEl) return;`) is one new line and MUST
        # NOT fire prefix-mod — pure-insert handles it correctly.
        # The computeMaskBBox shape (2 new `const W` / `const H`
        # declarations replacing 1 combined `const W = ..., H =
        # ...;` line) is the multi-line shape prefix-mod exists
        # for.
        if len(run_lines) < 2:
            return None

        # Preserve any base lines strictly between base_pos and
        # replace_at (blank separators, comments).
        result.extend(base_lines[base_pos:replace_at])
        result.extend(run_lines)
        base_pos = replace_at + 1
        saw_new_run = True

    result.extend(base_lines[base_pos:])

    if not saw_new_run:
        return None  # no modification found; not prefix-mod

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

    # NEW: prefix-modification semantics.  Fires before pure-insert.
    # Handles "replace line X with lines Y,Z; rest unchanged" — a
    # shape pure-insert cannot express (insert would duplicate X).
    modified = _try_prefix_modification(inner_prefix, base_scope_inner)
    if modified is not None:
        if modified == base_scope_inner:
            return base
        return base[:body_open] + modified + base[body_close:]

    # Comment-only suffix is documentation (the AI's explanation of
    # what was elided), not code to insert.  Ignore it.
    if _is_comment_only(inner_suffix):
        inner_suffix = []

    # Fall back to pure-insert semantics.
    base_stripped_set = {
        ln.strip() for ln in base_scope_inner.splitlines() if ln.strip()
    }

    new_prefix = [ln for ln in inner_prefix
                  if ln.strip() and ln.strip() not in base_stripped_set]
    new_suffix = [ln for ln in inner_suffix
                  if ln.strip() and ln.strip() not in base_stripped_set]

    if not new_prefix and not new_suffix:
        # All body lines are already in base — this is a no-op, not a
        # failure.  Return base unchanged; the caller detects it via
        # `result == content` and marks the patch `already-present`.
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
    """True iff `new_body` looks like a complete replacement of
    `base_scope_text`.

    The heuristic: if `base_scope_text` ends with a "closer"
    (`</details>` or `}`), `new_body` must end with the same closer.
    A prefix that omits the closer is a partial-splice candidate, not
    a full replacement.
    """
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
    """Splice `new_body`'s additions into `base_scope_text` line-wise.

    Uses `difflib.SequenceMatcher` to compute the line-level diff.
    Inserts and small replaces from `new_body` are applied; deletions
    from `base_scope_text` are skipped (a partial body shouldn't be
    silently removing content — that would be a full replace).

    BALANCE VALIDATION: after the splice, verify that the result has
    the same brace/paren/bracket balance as the base scope.  If not,
    refuse.  The pg_pixels.py failure case: block 99 ends with a
    *comment* (`// ... rest unchanged; ...`) instead of a `...`
    marker or the actual `}` closer.  SequenceMatcher treated the
    missing closer as a 'delete' from new, which was correctly
    skipped, but a subsequent 'replace' opcode still removed the
    base's closing brace.  The result had `+1` unclosed brace.
    Refusing here keeps the record at HIGH trust rather than
    fail-closing on a corruption the splice itself caused.

    Returns the spliced scope text, or None when the diff adds
    nothing (meaning the "new" body is a strict subset of the old —
    a truncation, not an extension), or when the result would be
    unbalanced.
    """
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
        # 'delete' — skipped deliberately
    if added_lines == 0:
        return None
    result_text = "\n".join(result)
    if result_text == base_scope_text:
        return None
    if _js_balance(result_text) != _js_balance(base_scope_text):
        return None
    return result_text


def _apply_scoped(base, scope_name, new_body):
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

        # Partial splice: when the new body is a *prefix* of the
        # base scope (missing the closer), splice the prefix in
        # rather than truncating the base.
        if not _new_body_is_complete_for_scope(
                base_scope_text, new_body):
            spliced = _try_scoped_partial_splice(
                base_scope_text, new_body)
            if spliced is None:
                # FIX 1: an incomplete body that also can't be
                # spliced is a fail-closed case, not a fall-through
                # to full replacement. A full replace with an
                # incomplete body always leaves the result
                # unbalanced (the base's closer is dropped but the
                # body's closer is absent), which corrupts the file
                # silently. This is the pg_pixels.py shape: block
                # 99's body ends with `// ... rest unchanged; ...`
                # instead of `}`.
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

        # Full replacement (original logic).
        if m_start < len(base) and base[m_start] == "\n":
            indent_pos = m_start + 1
            base_indent = ""
        else:
            indent_pos = base.rfind("\n", 0, m_start) + 1
            base_indent = base[indent_pos:m_start]
        # Consume the leading comment above the scope iff the
        # new_body carries its own comment.
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
