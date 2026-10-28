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
signature.

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
statements.

PRIOR-INSERTIONS SCOPE CONTEXT (2026-10-28e)
============================================
The prior-insertion fallback accepts an optional `prior_scopes` dict,
keyed by the prior line, mapping to the scope name extracted from
the Find block's broader prose. When the anchor appears more than
once in the base, the fallback uses `_anchor_within_scope` to find
the unique occurrence inside the recorded scope.  When the recorded
scope doesn't itself contain the anchor (but does call a helper
that does), `_anchor_within_callee` walks the scope's direct
`self.X(...)` callees to find the unique occurrence inside one of
them.

SCOPE-FIRST ORDERING (2026-10-28f)
==================================
The prior-insertion fallback now partitions `prior_insertions` into
**scoped** (lines with a `prior_scopes` entry) and **unscoped**, and
tries all scoped candidates before any unscoped one.

Motivating shape (comfyui.py idx=97 in production):
  · idx=95 landed a `"--output_rotation": {...}` entry inside
    `@command(args={...})`.
  · idx=96 was the Find block for `output_name = params.get(...)`;
    its broader prose ("find the `comfy_generate` command decorator")
    names `comfy_generate`, so `prior_scopes[output_name] =
    "comfy_generate"`.
  · idx=97 (try/except) tried to place.

Without scope-first ordering, the fallback iterates
`prior_insertions` in set order.  The args entry's `"help": "...}"}`
line is "safe" per `_anchor_line_is_safe` (closes but doesn't open),
so inserting after it puts the try/except INSIDE the args dict —
invalid Python.

With scope-first ordering, the scoped `output_name` candidate is
tried first: `_anchor_within_scope(base, output_name, comfy_generate)`
returns -1 (not in the method's own body), then
`_anchor_within_callee` walks `comfy_generate`'s callees and finds
`output_name` inside `_comfy_generate_impl`.  The try/except lands
correctly, and the args-entry lines are never tried as anchors.

SAFETY GATES ON PRIOR-INSERTION ANCHORS
=======================================
Two safety gates apply to every prior-insertion candidate:

  · `_anchor_line_is_safe` (from patch_predicates): refuse a line
    whose own text opens more brackets than it closes (e.g.,
    `"--output_rotation": {"type": "float", ...` — an unclosed
    dict opener).

  · `_anchor_is_in_expression_context` (NEW 2026-10-28f): for `.py`
    files, refuse a line whose position lies inside any unclosed
    `(`/`[`/`{`.  This catches the args-dict *closer* line
    (`"help": "..."},`) — safe per the opener check (closes, doesn't
    open), but still inside the args dict literal.

Together these gates make an args-dict line unusable as a prior-
insertion anchor regardless of which line the set-iteration order
picks first.

ANCHOR-LINE SAFETY (Fix 4)
==========================
Inserting AFTER a line that opens an unclosed block lands the body
*inside* that block.  `_anchor_line_is_safe` refuses such anchors;
the before-anchor heuristic (which has no such constraint) is what
places those bodies.

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
unique anchor.  When the anchor text appeared more than once, the
branch gave up and the patch failed.

The fix: when the anchor is non-unique, look for a *unique* occurrence
inside the scope named by the prose.  `_anchor_within_named_scope`
locates the scope via `patch_scope_insert._find_scope_body_range`,
finds all occurrences of the anchor inside it, and returns the single
one if exactly one exists.

CALL-ONLY ANCHORS FALL THROUGH (2026-10-28c)
============================================
The statement branch used to skip any anchor matching
`_CALL_ONLY_RE` (`func(`), assuming the earlier call-arg branch
handled it.  But the call-arg branch requires a *string literal*
argument (`func("x")`); a bare `func()` or `func(arg)` with no
literal falls through every branch and the patch fails.

The skip is removed.  `func()` shapes are now attempted by the
statement branch.

COMMENT-AWARE ANCHORS (2026-10-28d)
===================================
An anchor match that falls inside a comment is never a valid
insertion point.  Every anchor branch consults
`patch_comments.get_comment_spans(base, path)` and skips any
candidate position that falls inside a comment.
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
    AFTER the definition's whole body — at the def's own indent.
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
#  Scope-aware anchor helpers (2026-10-28b/c/d/e/f)
# ===========================================================================

def _anchor_within_scope(base, anchor_text, scope_name, spans=None):
    """Char position of `anchor_text` when it appears exactly once
    *in code* inside `scope_name`'s body; -1 otherwise.

    Fail-closed: if `scope_name` is empty, or the scope cannot be
    located, or the anchor appears zero or more than one time inside
    the scope *in code*, returns -1.
    """
    if not scope_name:
        return -1
    from .patch_scope_insert import _find_scope_body_range
    scope_range = _find_scope_body_range(base, scope_name)
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
                                spans=None):
    """Char position of `anchor_text` when it appears exactly once
    inside the scope named by the prose; -1 otherwise.
    """
    from .patch_scope_insert import _extract_scope_name_from_prose
    named_scope = _extract_scope_name_from_prose(context_before or "")
    return _anchor_within_scope(
        base, anchor_text, named_scope, spans=spans)


_CALLEE_CALL_RE = re.compile(r'self\.([A-Za-z_]\w*)\s*\(')


def _anchor_within_callee(base, anchor_text, scope_name, spans=None):
    """Char position of `anchor_text` when it appears exactly once
    *in code* inside one of `scope_name`'s direct `self.X(...)`
    callees; -1 otherwise.

    Motivating shape (comfyui.py idx=97): the prose names
    `comfy_generate` (the public @command method), but the anchor
    `output_name = params.get("output_name") or None` lives in
    `_comfy_generate_impl` — a private helper that `comfy_generate`
    delegates to.  `_anchor_within_scope` returns -1; this walks the
    callee graph one level deep and finds the anchor in
    `_comfy_generate_impl`.

    One level only — nested-callee traversal would risk finding an
    unrelated occurrence in a transitive callee.  If the anchor isn't
    in a direct callee's body, returns -1 and the caller falls
    through.

    Fail-closed on ambiguous placement (multiple callees with the
    anchor, or a single callee with multiple occurrences).
    """
    if not scope_name:
        return -1
    from .patch_scope_insert import _find_scope_body_range
    scope_range = _find_scope_body_range(base, scope_name)
    if scope_range is None:
        return -1
    scope_start, scope_end, _ = scope_range
    scope_body = base[scope_start:scope_end]

    callee_names = set(_CALLEE_CALL_RE.findall(scope_body))
    if not callee_names:
        return -1

    matches = []
    for name in sorted(callee_names):
        callee_range = _find_scope_body_range(base, name)
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


# ===========================================================================
#  Expression-context refusal (2026-10-28f)
# ===========================================================================

def _anchor_is_in_expression_context(base, pos, path):
    """True if `pos` is inside an unclosed bracket group in a file
    where brackets denote *expressions* rather than *blocks*.

    For `.py` files: any unclosed `(` / `[` / `{` at `pos` means the
    anchor is inside an expression — function-call args, dict literal,
    list literal — rather than at a statement boundary.  Inserting a
    statement there produces invalid Python.

    Motivating incident (comfyui.py idx=97, production): a prior
    insertion landed `"--output_rotation": {...}` inside
    `@command(args={...})`.  `_anchor_line_is_safe` correctly refused
    the args-opener line, but the entry's `"help": "..."},` closer is
    "safe" (opens nothing, closes one) — inserting after it puts the
    try/except INSIDE the args dict, producing invalid syntax.  The
    expression-context check refuses the closer too (the closer is
    still inside an unclosed `(`/`{`).

    For `.js` / `.ts` / etc.: returns False.  Block braces and object
    braces are indistinguishable without a proper parser; the existing
    `_anchor_line_is_safe` check is what protects those cases.
    """
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
                              prior_insertions=None, path="",
                              prior_scopes=None):
    """Insert `body` after the most specific anchor the prose (or the
    prior insertions) supplies.

    `prior_insertions` is an optional set of stripped lines that
    earlier patches in the same batch inserted.  `prior_scopes`
    (optional) is `dict[stripped_line -> scope_name]` that
    disambiguates a repeated anchor.  See the module docstring,
    PRIOR-INSERTIONS SCOPE CONTEXT and SCOPE-FIRST ORDERING sections.
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

    # ---- Prior-insertion fallback: SCOPE-FIRST ORDERING (2026-10-28f) ----
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

        # Pass 1: scoped candidates.  Deterministic order so repeated
        # runs produce identical output.  Resolves the anchor via the
        # recorded scope's body, then (if not found there) via its
        # direct `self.X(...)` callees.
        for stripped in sorted(scoped_priors):
            scope = prior_scopes[stripped]
            idx = _anchor_within_scope(base, stripped, scope, spans=spans)
            if idx == -1:
                idx = _anchor_within_callee(
                    base, stripped, scope, spans=spans)
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

        # Pass 2: unscoped candidates.  Only unique anchors are
        # considered (`n == 1`); ambiguous ones are skipped (nothing
        # tells us which occurrence to prefer).  Every candidate must
        # pass BOTH safety gates:
        #   · `_anchor_line_is_safe`: the line doesn't open more
        #     brackets than it closes.
        #   · `_anchor_is_in_expression_context`: for .py files, the
        #     position isn't inside any unclosed `(`/`[`/`{`.
        # The second gate is what prevents the args-dict *closer*
        # line (`"help": "..."},`) from being a prior-insertion
        # anchor.
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
