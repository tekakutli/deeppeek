"""
patch_anchor_insert — insert relative to a named anchor line (FIX 3).

Three heuristics that place a body at a *position* named by prose
("before `X`", "between `X` and `Y`", "after `X`"):

    _try_insert_before_anchor     "before the `X` field"
    _try_insert_between_anchors   "between the `Image` and `Masks` blocks"
    _try_insert_after_anchor      "after `some_code_call();`"

MEDIUM CHURN: these three are the practical workhorses for edits
that name an existing line or block.  They evolve as real
conversations surface new prose forms.  Isolating them means
adjusting a prose regex or adding a new anchor shape does not
require reading the scope-insert, replace, or structural-append
code.

ANCHOR-LINE SAFETY (Fix 4)
==========================
Inserting AFTER a line that opens an unclosed block lands the body
*inside* that block.  The pg_panel_scrubs.py failure case: the only
anchor for the `viewRotation` field was
`pgkitRegisterField("status.running", {` — an object-literal
opener.  Inserting after it puts the new field inside the
`status.running` registration; the resulting file is not the
intended edit.  `_anchor_line_is_safe` (imported from
patch_predicates) refuses such anchors; the before-anchor heuristic
(which has no such constraint) is what places those bodies.

MULTI-OCCURRENCE ANCHORS (Fix 5)
================================
An anchor identifier may appear more than once — most commonly,
once in a docstring (prose describing the identifier) and once in
the actual code.  The pg_panel_scrubs.py failure case: the prose
says "before the `status.running` field"; `status.running` appears
once in the module docstring and once in the `pgkitRegisterField`
call.  `_try_insert_before_anchor` collects ALL occurrences, tries
them right-to-left (bottom-of-file first), and skips each one that
(a) is a closer-only line, or (b) has no strong code signal.
Fail-closed: if no occurrence passes both checks, returns None.
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


# ===========================================================================
#  Insert-before-anchor (FIX 3 + FIX 5)
# ===========================================================================

_BEFORE_PROSE_RE = re.compile(
    r'\bbefore\b[^.\n]{0,120}?`([^`\n]+)`',
    re.IGNORECASE,
)


def _try_insert_before_anchor(base, body, context_before):
    """Insert `body` before the line named by the most recent
    "before `X`" phrase in the prose.

    Called from `_try_deterministic_patch_inner` before the
    after-anchor heuristic so that "before X" is preferred over
    "after X" when the prose supports either reading.  Returns the
    patched base, or None.
    """
    if not body or not body.strip():
        return None
    body_stripped = body.strip()
    if not _body_is_complete_at_boundary(body_stripped):
        return None

    matches = _BEFORE_PROSE_RE.findall(context_before or "")
    if not matches:
        return None

    for anchor_text in reversed(matches):
        anchor_text = anchor_text.strip()
        if len(anchor_text) < 6:
            continue

        # Collect every occurrence of the anchor.  Multi-occurrence
        # is common: docstrings often mention the identifier in
        # prose while the actual code is much lower in the file.
        positions = []
        start = 0
        while True:
            p = base.find(anchor_text, start)
            if p == -1:
                break
            positions.append(p)
            start = p + 1
        if not positions:
            continue

        # Try from bottom-of-file to top-of-file.  "Before the X
        # field" most often means the bottom-most declaration.
        for idx in reversed(positions):
            line_start = base.rfind("\n", 0, idx) + 1
            line_end = base.find("\n", line_start)
            if line_end == -1:
                line_end = len(base)
            line = base[line_start:line_end]

            # Refuse if the anchor line is JUST a closer — the body
            # would land inside the block that just closed.
            if _line_is_closer_only(line):
                continue

            # Refuse if the line has no strong code signal — likely
            # a prose/docstring mention, not a usable code anchor.
            # This guard prevents inserting into a module docstring
            # when the identifier only appears in prose.
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
#  Insert-between-anchors (FIX 3)
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
    None.

    Handles three block shapes:
      · `<details><summary>name</summary>…</details>`
      · `(export )?(const|let|var) name = { … };`
      · `(export )?(async )?(function|def|class) name … { … }`
    """
    if not name:
        return None

    # <details> block by summary text
    pat_details = re.compile(
        r'<details\b[^>]*>\s*<summary>\s*'
        + re.escape(name) + r'\s*</summary>.*?</details>',
        re.DOTALL | re.IGNORECASE)
    m = pat_details.search(base)
    if m:
        return (m.start(), m.end())

    # Object literal by identifier
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
                # Consume a trailing `;` if present.
                end = close_idx + 1
                if end < len(base) and base[end] == ";":
                    end += 1
                return (m.start(), end)

    # Function / class by name
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


def _try_insert_between_anchors(base, body, context_before):
    """Insert `body` between the two blocks named in a
    "between X and Y <what>" phrase in the prose.

    Returns the patched base, or None.
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

    # Order them: A must end before B starts.  If the prose said
    # "between B and A", the blocks still appear in file order; swap
    # here so the insert point is B's start.
    if range_a[1] <= range_b[0]:
        insert_at_char = range_b[0]
    elif range_b[1] <= range_a[0]:
        insert_at_char = range_a[0]
    else:
        # Overlapping — prose is ambiguous or the extractor matched
        # the wrong blocks.  Fail-closed.
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
#  Insert-after-anchor
# ===========================================================================

_PROSE_BACKTICK_IDENT_RE = re.compile(r'`([A-Za-z_$][\w$]*)`')
_PROSE_CALL_ARG_RE = re.compile(
    r'`([A-Za-z_$][\w$]*)\s*\(\s*["\']([^"\']+)["\']')
_BACKTICK_CONTENT_RE = re.compile(r'`([^`\n]+)`')
_IDENT_ONLY_RE = re.compile(r'^[A-Za-z_$][\w$]*$')
_CALL_ONLY_RE = re.compile(r'^[A-Za-z_$][\w$]*\s*\(')


def _try_insert_after_anchor(base, body, context_before):
    body_stripped = body.strip()
    if not body_stripped:
        return None
    # Boundary completeness — see module docstring.  A fragment whose
    # last non-blank line ends with `,` is a continuation, not a
    # complete statement; landing it between two lines corrupts both.
    if not _body_is_complete_at_boundary(body_stripped):
        return None
    body_lines = body_stripped.splitlines()
    if len(body_lines) < 2 or len(body_lines) > 40:
        return None

    call_matches = _PROSE_CALL_ARG_RE.findall(context_before)
    for func_name, arg_str in reversed(call_matches):
        pattern_str = func_name + '("' + arg_str
        idx = base.find(pattern_str)
        if idx == -1:
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

    ident_matches = _PROSE_BACKTICK_IDENT_RE.findall(context_before)
    for ident in reversed(ident_matches):
        if_re = re.compile(
            r'(?:^|\n)\s*if\s*\(\s*' + re.escape(ident) + r'\s*\)\s*\{',
            re.MULTILINE)
        if_match = if_re.search(base)
        if if_match:
            block_open = base.rfind("{", if_match.start(), if_match.end())
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
            r'(?:^|\n)\s*(?:const|let|var)\s+' + re.escape(ident) +
            r'\s*=[^\n]*;', re.MULTILINE)
        const_match = const_re.search(base)
        if const_match:
            line_end = base.find("\n", const_match.end())
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1
            # Extend through a following `if (ident) { ... }` block.
            extended_idx = _find_identifier_block_end_line(base, ident)
            if extended_idx is not None:
                base_lines_local = base.split("\n")
                extended_offset = sum(
                    len(base_lines_local[k]) + 1
                    for k in range(extended_idx + 1))
                if extended_offset > line_end and extended_offset <= len(base):
                    line_end = extended_offset
            insertion = body_stripped.rstrip() + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

    statement_matches = _BACKTICK_CONTENT_RE.findall(context_before)
    for raw in reversed(statement_matches):
        stripped = raw.strip()
        if len(stripped) < 6:
            continue
        if _IDENT_ONLY_RE.match(stripped):
            continue
        if _CALL_ONLY_RE.match(stripped):
            continue
        if base.count(stripped) != 1:
            continue

        idx = base.find(stripped)
        line_start = base.rfind("\n", 0, idx) + 1
        line_lead = ""
        p_scan = line_start
        while p_scan < len(base) and base[p_scan] in " \t":
            line_lead += base[p_scan]
            p_scan += 1
        anchor_indent = line_lead

        # Fix 4: refuse if the anchor line opens an unclosed block.
        # Inserting after it would land inside.  The before-anchor
        # heuristic (which has no such constraint) is the correct
        # placement for these anchors — e.g. "before the
        # `status.running` field" when the anchor line is
        # `pgkitRegisterField("status.running", {`.
        if not _anchor_line_is_safe(base, line_start):
            continue

        line_end = base.find("\n", idx)
        if line_end == -1:
            line_end = len(base)
        else:
            line_end += 1

        # NB: pass the un-stripped body (only trimming blank leading /
        # trailing lines) so `_reindent_body` computes the relative
        # indent from the first content line's ORIGINAL indent.  Passing
        # `body_stripped` (whose first line is 0-indented because
        # `.strip()` removed the leading whitespace) makes
        # `orig_indent_len = 0`, so every subsequent line gets its full
        # original indent added on top of `anchor_indent`.
        reindented = _reindent_body(anchor_indent,
                                    body.lstrip("\n").rstrip())
        insertion = reindented.rstrip() + "\n"
        result = base[:line_end] + insertion + base[line_end:]
        if result != base:
            return result

    return None
