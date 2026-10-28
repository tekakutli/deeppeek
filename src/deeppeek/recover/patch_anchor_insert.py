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
that name a def/class/function.  Prose like "Add two helpers near
`_decode_png`:" had no anchor before this branch.

The branch inserts at the *sibling* indent of the named definition:
a `def`/`class`/`function` body inserted "near" another declaration
belongs next to it, not inside it.  A body that is *not* a
declaration is refused — inner statements are the
`_try_insert_into_named_scope` path's job.

Multi-line signatures (`def foo(\n a,\n b,\n):`) are handled by
`_find_python_sig_end`: it scans forward from the `def` keyword,
tracking paren/bracket/brace depth and string state, and returns the
position of the first `:` at depth 0 — the colon that terminates the
signature.  Without this, a signature whose closing `):` sits on its
own line at the def's indent would be mistaken for the body's end,
and the body would be inserted before it.

PRIOR-INSERTIONS FALLBACK (2026-10-28)
======================================
When the prose names no backtick anchor at all, but an earlier patch
in the same batch already inserted a line that appears (uniquely) in
the current base, insert after that line.  This covers the
"Find: X / Add just after: Y" pattern where X is pre-existing and Y's
only anchor is X's line in the (unchanged) base.

The prior-insertion fallback is the *only* branch that accepts a
single-line body.  The backtick-anchored branches (call, identifier,
statement) require 2+ lines — they are meant for blocks, not single
statements.  Single-line inserts after a prior line are legitimate
(the exact "add this after X" case) and must fire when the fallback's
preconditions are met.

ANCHOR-LINE SAFETY (Fix 4)
==========================
Inserting AFTER a line that opens an unclosed block lands the body
*inside* that block.  `_anchor_line_is_safe` (imported from
patch_predicates) refuses such anchors; the before-anchor heuristic
(which has no such constraint) is what places those bodies.

MULTI-OCCURRENCE ANCHORS (Fix 5)
================================
An anchor identifier may appear more than once — most commonly, once
in a docstring and once in the actual code.
`_try_insert_before_anchor` collects ALL occurrences, tries them
right-to-left (bottom-of-file first), and skips each one that (a) is
a closer-only line, or (b) has no strong code signal.  Fail-closed
if no occurrence passes both checks.

SCOPE-AWARE ANCHOR SELECTION (2026-10-28b)
==========================================
`_try_insert_after_anchor`'s statement branch used to require a
unique anchor (`base.count(anchor) != 1 -> continue`).  When the
anchor text appeared more than once (typically because one occurrence
was in the target scope and another was added by an earlier patch),
the branch gave up and the patch failed.

The fix: when the anchor is non-unique, look for a *unique* occurrence
inside the scope named by the prose.  `_anchor_within_named_scope`
locates the scope via `patch_scope_insert._find_scope_body_range`,
finds all occurrences of the anchor inside it, and returns the single
one if exactly one exists.  This resolves the case where the prose
says e.g. "add `foo();` after the existing call in `handler()`" and
`foo();` was already inserted by an earlier patch into a *different*
function.

Anchors that remain non-unique even within the named scope still
fail — the branch does not fall back to "guess which one you meant."

CALL-ONLY ANCHORS FALL THROUGH (2026-10-28c)
============================================
The statement branch used to skip any anchor matching
`_CALL_ONLY_RE` (`func(`), assuming the earlier call-arg branch
handled it.  But the call-arg branch requires a *string literal*
argument (`func("x")`); a bare `func()` or `func(arg)` with no
literal falls through every branch and the patch fails.

The skip is removed.  `func()` shapes are now attempted by the
statement branch, which:
  · uses the unique-anchor path when the call appears once,
  · uses the scope-aware path when it appears once inside the
    named scope,
  · falls through (`_anchor_line_is_safe` refuses openers, and
    the position-count checks refuse genuinely ambiguous positions).

The call-arg branch still runs *first* and still owns
`func("literal")` — insertion after the closing paren of a
multi-line call.  The statement branch is the fallback for shapes
the call-arg branch does not recognize.

COMMENT-AWARE ANCHORS (2026-10-28d)
===================================
An anchor match that falls inside a comment is never a valid
insertion point.  The anchoring heuristics used to find the anchor
text and insert right there — regardless of whether the text was in
the actual code or in a `/* ... */` block comment, a `//` line
comment, a Python `#` comment, or a triple-quoted docstring.

Motivating case: the pg_panel_scrubs.py `_clearImageAction` fragment
(block idx=72) was placed after `pgkitNotifyAll() there already
includes this field.`, which sits inside the `/* ... */` docstring-
block comment for `masks.selectedIndex`.  The file compiled, the AST
parsed, the record reconciled to MEDIUM, and the fragment was dead
code in a comment.

Every anchor branch (`_try_insert_before_anchor`,
`_try_insert_between_anchors`, `_try_insert_after_anchor`) consults
`patch_comments.get_comment_spans(base, path)` and skips any
candidate position that falls inside a comment.  Position counting
for the "unique in base" test happens *after* filtering — an anchor
that appears once in code and once in a comment is treated as
unique-in-code, so the code occurrence wins.

The `path` parameter is threaded from `_try_deterministic_patch`
(the only caller with the file path).  Callers that omit `path` get
the "unknown extension, try both syntaxes" fallback from
`get_comment_spans`, which is the safe over-detection default.
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


# ===========================================================================
#  Insert-before-anchor (FIX 3 + FIX 5 + comment-aware)
# ===========================================================================

_BEFORE_PROSE_RE = re.compile(
    r'\bbefore\b[^.\n]{0,120}?`([^`\n]+)`',
    re.IGNORECASE,
)


def _try_insert_before_anchor(base, body, context_before, path=""):
    """Insert `body` before the line named by the most recent
    "before `X`" phrase in the prose.

    Called from `_try_deterministic_patch_inner` before the
    after-anchor heuristic so that "before X" is preferred over
    "after X" when the prose supports either reading.  Returns the
    patched base, or None.

    Positions inside comments are skipped.  See the module docstring,
    COMMENT-AWARE ANCHORS section.
    """
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


# ===========================================================================
#  Insert-between-anchors (FIX 3 + comment-aware)
# ===========================================================================

_BETWEEN_RE = re.compile(
    r'\bbetween\b[^.\n]{0,120}?'
    r'(?:the\s+)?([A-Za-z][\w ]{1,50}?)\s+(?:and|&)\s+'
    r'(?:the\s+)?([A-Za-z][\w ]{1,50}?)\s+'
    r'(?:<[^>]+>|blocks?|sections?|fields?|functions?|elements?)',
    re.IGNORECASE,
)


def _find_named_block_end(base, name):
    """Return (start_char, end_char) of the block named `name`, or
    None.  Handles `<details>`, object literals, function/class defs."""
    if not name:
        return None

    pat_details = re.compile(
        r'<details\b[^>]*>\s*<summary>\s*'
        + re.escape(name) + r'\s*</summary>.*?</details>',
        re.DOTALL | re.IGNORECASE)
    m = pat_details.search(base)
    if m:
        return (m.start(), m.end())

    pat_obj = re.compile(
        r'(?:^|\n)(?:export\s+)?(?:const|let|var)\s+'
        + re.escape(name) + r'\s*=\s*\{',
        re.MULTILINE)
    m = pat_obj.search(base)
    if m:
        open_idx = base.find("{", m.end() - 1)
        if open_idx != -1:
            close_idx = _find_matching_brace(
                base, open_idx, is_python=False)
            if close_idx != -1:
                end = close_idx + 1
                if end < len(base) and base[end] == ";":
                    end += 1
                return (m.start(), end)

    pat_fn = re.compile(
        r'(?:^|\n)(?:export\s+)?(?:async\s+)?'
        r'(?:function|def|class)\s+' + re.escape(name) + r'\b',
        re.MULTILINE)
    m = pat_fn.search(base)
    if m:
        open_idx = base.find("{", m.end())
        if open_idx != -1:
            close_idx = _find_matching_brace(
                base, open_idx, is_python=False)
            if close_idx != -1:
                return (m.start(), close_idx + 1)

    return None


def _try_insert_between_anchors(base, body, context_before, path=""):
    """Insert `body` between the two blocks named in a
    "between X and Y <what>" phrase in the prose.

    Comment-embedded block ends are refused.  See the module
    docstring, COMMENT-AWARE ANCHORS section.
    """
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


# ===========================================================================
#  Python signature-end detection
# ===========================================================================

def _find_python_sig_end(base, start_pos):
    """Return the position just after the `:` that closes the
    signature of a Python def/class starting at `start_pos`.

    Scans forward, tracking paren/bracket/brace depth and string
    state, until a `:` at depth 0 is found outside any string.  This
    correctly handles:

      · single-line sig:     `def foo(a):`
      · multi-line sig:      `def foo(\\n a,\\n b,\\n):`
      · type annotations:    `def foo(a: int) -> str:`
        (the first `:` in `a: int` is inside parens — depth > 0)
      · default dicts:       `def foo(x={"a": 1}):`
        (the `:` inside the dict is inside braces — depth > 0)

    Returns -1 if no terminating `:` is found (malformed input).
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


# ===========================================================================
#  Insert-after-def
# ===========================================================================

_DECL_START_PREFIXES = (
    "def ", "async def ", "class ",
    "function ", "async function ", "export ",
    "@",
)


def _try_insert_after_def(base, ident, body):
    """If `ident` names a def/class/function in `base`, insert `body`
    AFTER the definition's whole body — at the def's own indent
    (sibling level).

    Refuses when the body is not itself a declaration (does not start
    with `def`/`class`/`function`/`export`/`@`).  Inner statements
    belong to `_try_insert_into_named_scope`, not here: a statement
    at sibling indent would be a Python `unexpected indent` error.

    Python sig detection uses `_find_python_sig_end` so multi-line
    signatures (`def foo(\\n a,\\n b,\\n):`) don't fool the scan into
    inserting before the signature's closing line.

    Returns the patched base or None.
    """
    if not ident or not body or not body.strip():
        return None

    body_lines = body.lstrip("\n").rstrip().split("\n")
    first_body_line = next(
        (ln for ln in body_lines if ln.strip()), "")
    first_stripped = first_body_line.lstrip()
    if not first_stripped.startswith(_DECL_START_PREFIXES):
        return None

    # ---- Python ----
    py_re = re.compile(
        r'(?m)^([ \t]*)(?:async\s+)?(?:def|class)\s+'
        + re.escape(ident) + r'\b'
    )
    m = py_re.search(base)
    if m:
        def_indent = m.group(1)
        sig_end = _find_python_sig_end(base, m.start())
        if sig_end == -1:
            return None
        scan = base.find("\n", sig_end)
        if scan == -1:
            return None
        scan += 1
        insert_at = len(base)
        while scan < len(base):
            nl = base.find("\n", scan)
            if nl == -1:
                nl = len(base)
            line = base[scan:nl]
            if line.strip():
                line_indent = len(line) - len(line.lstrip())
                if line_indent <= len(def_indent):
                    insert_at = scan
                    break
            scan = nl + 1
        reindented = _reindent_body(def_indent, body.lstrip("\n").rstrip())
        insertion = reindented.rstrip() + "\n\n"
        result = base[:insert_at] + insertion + base[insert_at:]
        return result if result != base else None

    # ---- JS function / class ----
    js_re = re.compile(
        r'(?m)^([ \t]*)(?:export\s+)?(?:async\s+)?'
        r'(?:function|class)\s+' + re.escape(ident) + r'\b'
    )
    m = js_re.search(base)
    if m:
        brace_open = base.find("{", m.end())
        if brace_open == -1:
            return None
        brace_close = _find_matching_brace(base, brace_open)
        if brace_close == -1:
            return None
        line_end = base.find("\n", brace_close)
        if line_end == -1:
            line_end = len(base)
        else:
            line_end += 1
        sibling_indent = m.group(1)
        reindented = _reindent_body(
            sibling_indent, body.lstrip("\n").rstrip())
        insertion = reindented.rstrip() + "\n"
        result = base[:line_end] + insertion + base[line_end:]
        return result if result != base else None

    return None


# ===========================================================================
#  Scope-aware anchor helper (2026-10-28b, extended 2026-10-28d)
# ===========================================================================

def _anchor_within_named_scope(base, anchor_text, context_before,
                                spans=None):
    """Return the char position of `anchor_text` if it appears exactly
    once *in code* inside the scope named by the prose.  Returns -1
    otherwise.

    The named scope comes from `_extract_scope_name_from_prose`.  The
    helper exists to break a tie: when an anchor appears multiple times
    in the file, one occurrence usually lives inside the scope the
    prose names, and the others are in unrelated scopes (or added by
    an earlier patch that mis-placed the same anchor).  Picking the
    in-scope occurrence lands the insert at the semantically-correct
    position.

    `spans` (optional) is a comment-span list from
    `patch_comments.get_comment_spans`.  When provided, positions
    inside comments are filtered out before the unique-occurrence
    check.  This is the 2026-10-28d extension: without it, an anchor
    that appears once in code and once in an in-scope comment would
    be reported as ambiguous and the placement would fail.

    Fail-closed: if the prose names no scope, or the scope cannot be
    located, or the anchor appears more (or fewer) than once inside
    the scope *in code*, returns -1 so the caller falls through to
    its existing failure path.
    """
    from .patch_scope_insert import (
        _extract_scope_name_from_prose, _find_scope_body_range,
    )

    named_scope = _extract_scope_name_from_prose(context_before or "")
    if not named_scope:
        return -1

    scope_range = _find_scope_body_range(base, named_scope)
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


# ===========================================================================
#  Insert-after-anchor
# ===========================================================================

_PROSE_BACKTICK_IDENT_RE = re.compile(r'`([A-Za-z_$][\w$]*)`')
_PROSE_CALL_ARG_RE = re.compile(
    r'`([A-Za-z_$][\w$]*)\s*\(\s*["\']([^"\']+)["\']')
_BACKTICK_CONTENT_RE = re.compile(r'`([^`\n]+)`')
_IDENT_ONLY_RE = re.compile(r'^[A-Za-z_$][\w$]*$')
_CALL_ONLY_RE = re.compile(r'^[A-Za-z_$][\w$]*\s*\(')


def _try_insert_after_anchor(base, body, context_before,
                              prior_insertions=None, path=""):
    """Insert `body` after the most specific anchor the prose (or the
    prior insertions) supplies.

    `prior_insertions` is an optional set of stripped lines that
    earlier patches in the same batch inserted.  When the prose has
    no backtick anchor, the last prior insertion line present
    uniquely in `base` becomes the anchor.

    The backtick-anchored branches (call, identifier, statement)
    require a 2+ line body; they are designed for blocks.  The
    prior-insertion fallback accepts any length — single-line
    "add this after X" is exactly its use case.

    Positions inside comments are skipped in every branch.  See the
    module docstring, COMMENT-AWARE ANCHORS section.

    The statement branch does NOT skip `func(`-shaped anchors: the
    call-arg branch that handles `func("literal")` requires a string
    literal, and a bare `func()` or `func(arg)` needs a fallback.
    See the module docstring, CALL-ONLY ANCHORS FALL THROUGH
    section.
    """
    body_stripped = body.strip()
    if not body_stripped:
        return None
    if not _body_is_complete_at_boundary(body_stripped):
        return None
    body_lines = body_stripped.splitlines()
    if len(body_lines) > 40:
        return None

    spans = get_comment_spans(base, path)

    # ---- Primary branches: 2+ line bodies only ----
    if len(body_lines) >= 2:
        # Call form: `func("arg")`.
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

        # Identifier-only anchor: names a def/class/function, or a
        # const/if pair.
        ident_matches = _PROSE_BACKTICK_IDENT_RE.findall(
            context_before or "")
        for ident in reversed(ident_matches):
            # def/class/function branch first.
            def_result = _try_insert_after_def(base, ident, body)
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

        # Statement anchor: a backticked statement line.
        statement_matches = _BACKTICK_CONTENT_RE.findall(
            context_before or "")
        for raw in reversed(statement_matches):
            stripped = raw.strip()
            if len(stripped) < 6:
                continue
            if _IDENT_ONLY_RE.match(stripped):
                # Identifier-only anchors are the identifier branch's
                # job.  They were tried above; if they failed, the
                # statement branch must not fire (inserting after a
                # def header at column 0 nests a body incorrectly).
                continue
            # NOTE (2026-10-28c): the `_CALL_ONLY_RE` skip was removed
            # here.  See the module docstring, CALL-ONLY ANCHORS FALL
            # THROUGH section.

            # SCOPE-AWARE ANCHOR SELECTION (2026-10-28b) +
            # COMMENT-AWARE ANCHORS (2026-10-28d):
            # Find all non-comment positions of the anchor.  If
            # exactly one, use it.  If multiple, look for the one
            # inside the named scope.  Filter comment positions
            # first so a `/* ... */`-embedded occurrence doesn't
            # count against uniqueness.
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
                    base, stripped, context_before, spans=spans)
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

    # ---- Prior-insertion fallback: any body length ----
    if prior_insertions:
        for prior in prior_insertions:
            stripped = prior.strip()
            if not stripped or len(stripped) < 8:
                continue
            if base.count(stripped) != 1:
                continue
            idx = base.find(stripped)
            if is_inside_comment(idx, spans):
                continue
            line_start = base.rfind("\n", 0, idx) + 1
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
