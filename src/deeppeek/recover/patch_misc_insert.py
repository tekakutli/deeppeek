"""
patch_misc_insert — context insert and object-entry insert.

Two smaller insert heuristics:

  _try_context_insert      2–30 lines, unique first-or-last-line
                           anchor; body inserted adjacent to that
                           anchor line
  _try_object_entry_insert 2–5 lines of `key: value,` entries; prose
                           names a function like `_get()`, the
                           entries land inside its innermost object
                           literal

MEDIUM-STABLE: these have specific, well-defined shapes and rarely
need to grow.  Isolating them keeps the churn-heavy scope and anchor
heuristics from crowding this code in review.
"""

import re

from .patch_predicates import _context_insert_ok
from .patch_text import _find_matching_brace


# ===========================================================================
#  Context insert
# ===========================================================================

_CONTEXT_INSERT_MAX_LINES = 30
_CONTEXT_INSERT_MIN_ANCHOR_CHARS = 8

# A "structural line" is a bare closing delimiter — `}`, `});`, `};`,
# `]`, `])` etc., optionally followed by `;` or `,`.  These appear in
# every JS file at the end of every block, so `stripped not in base` is
# always False for them (base contains `}` somewhere).  Dropping them
# while keeping the corresponding opener is what silently corrupted the
# pg_panel_fields.py case: `_filter_new` was treating a `}` from a
# balanced body as "already present, remove it" — leaving the inserted
# `if (...) {` with no closer.
_STRUCTURAL_LINE_RE = re.compile(
    r"^\s*"
    r"[}\])]+"
    r"[;,)]*"
    r"\s*$"
)


def _try_context_insert(base, body):
    if not body or not body.strip():
        return None
    body_lines = body.rstrip("\n").split("\n")
    if len(body_lines) < 2 or len(body_lines) > _CONTEXT_INSERT_MAX_LINES:
        return None

    first_stripped = body_lines[0].strip()
    if re.match(r'^(?:export\s+)?(?:const|let|var|function|class)\s+\w+',
                first_stripped):
        return None
    if re.match(r'^(?:def|class)\s+\w+', first_stripped):
        return None
    if first_stripped.startswith("<details") or "<summary>" in body[:200]:
        return None
    # FIX: a scope-shaped decorated method (`@command(...)` at non-zero
    # indent) must not be context-inserted inline — it wants to land
    # inside the container named by prose, handled by
    # `_try_insert_into_named_scope`.  Context insert would otherwise
    # nest it inside whatever existing block the anchor heuristic finds.
    if first_stripped.startswith("@"):
        first_indent = len(body_lines[0]) - len(body_lines[0].lstrip())
        if first_indent > 0:
            return None

    def _unique(line):
        stripped = line.strip()
        return (len(stripped) >= _CONTEXT_INSERT_MIN_ANCHOR_CHARS
                and base.count(stripped) == 1)

    def _absent(line):
        stripped = line.strip()
        if not stripped:
            return True
        if _STRUCTURAL_LINE_RE.match(stripped):
            return True
        return stripped not in base

    def _filter_new(lines):
        return [ln for ln in lines if _absent(ln)]

    def _reindent_to(new_lines, anchor_indent):
        if not new_lines:
            return []
        first_indent_len = None
        for ln in new_lines:
            if ln.strip():
                first_indent_len = len(ln) - len(ln.lstrip(" \t"))
                break
        if first_indent_len is None:
            first_indent_len = 0
        out = []
        for ln in new_lines:
            if not ln.strip():
                out.append("")
                continue
            ln_indent = len(ln) - len(ln.lstrip(" \t"))
            relative = max(0, ln_indent - first_indent_len)
            out.append(anchor_indent + " " * relative + ln.lstrip(" \t"))
        return out

    last_line = body_lines[-1]
    if _unique(last_line) and _context_insert_ok(last_line, body):
        candidates = _filter_new(body_lines[:-1])
        if candidates:
            anchor_stripped = last_line.strip()
            anchor_idx = base.find(anchor_stripped)
            line_start = base.rfind("\n", 0, anchor_idx) + 1
            anchor_indent = base[line_start:anchor_idx]
            new_lines = _reindent_to(candidates, anchor_indent)
            insertion = "\n".join(new_lines) + "\n"
            result = base[:line_start] + insertion + base[line_start:]
            if result != base:
                return result

    first_line = body_lines[0]
    # The "insert body[1:] after a unique first line" strategy only
    # works when the first line is a STANDALONE statement.  If the
    # first line is a block opener, body[1:] contains the closer for
    # that opener — inserting body[1:] after base's matching opener
    # means the inserted closer closes base's opener, and base's
    # original closer becomes unmatched (-1 brace).  Refuse and let
    # the caller fall through to `_try_insert_after_anchor`.
    first_line_opens = first_line.count("{") - first_line.count("}")
    if (first_line_opens <= 0 and _unique(first_line)
            and _context_insert_ok(first_line, body)):
        candidates = _filter_new(body_lines[1:])
        if candidates:
            anchor_stripped = first_line.strip()
            anchor_idx = base.find(anchor_stripped)
            line_end = base.find("\n", anchor_idx)
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1
            anchor_indent = (
                first_line[:len(first_line) - len(first_line.lstrip(" \t"))])
            new_lines = _reindent_to(candidates, anchor_indent)
            insertion = "\n".join(new_lines) + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

    return None


# ===========================================================================
#  Object-entry insert
# ===========================================================================

_OBJECT_ENTRY_RE = re.compile(
    r'^\s*["\']?[A-Za-z_$][\w$]*["\']?\s*:\s*.+,?\s*$')
_PROSE_IDENTIFIER_RE = re.compile(r'`([A-Za-z_$][\w$]*)\s*\(\s*\)`')
_PROSE_IDENTIFIER_ALT_RE = re.compile(r'\b([A-Za-z_$][\w$]*)\s*\(\s*\)')


def _try_object_entry_insert(base, body, context_before):
    body_lines = [ln for ln in body.strip().splitlines() if ln.strip()]
    if not (2 <= len(body_lines) <= 5):
        return None
    if not all(_OBJECT_ENTRY_RE.match(ln) for ln in body_lines):
        return None

    matches = _PROSE_IDENTIFIER_RE.findall(context_before)
    if not matches:
        matches = _PROSE_IDENTIFIER_ALT_RE.findall(context_before)
    if not matches:
        return None
    ident = matches[-1]

    func_re = re.compile(
        r'(?:^|\n)\s*(?:export\s+)?(?:async\s+)?'
        r'(?:function|def)\s+' + re.escape(ident) + r'\s*[\(\{]',
        re.MULTILINE)
    func_match = func_re.search(base)
    if not func_match:
        return None

    func_body_open = base.find("{", func_match.end() - 1)
    if func_body_open == -1:
        return None
    func_body_close = _find_matching_brace(base, func_body_open, is_python=False)
    if func_body_close == -1:
        return None

    func_body = base[func_body_open:func_body_close]
    assign_re = re.compile(r'=\s*\{')
    assign_matches = list(assign_re.finditer(func_body))
    if not assign_matches:
        return None

    last_assign = assign_matches[-1]
    obj_open = func_body.find("{", last_assign.start()) + func_body_open
    obj_close = _find_matching_brace(base, obj_open, is_python=False)
    if obj_close == -1:
        return None

    line_start = base.rfind("\n", 0, obj_close) + 1
    obj_indent = base[line_start:obj_close]
    entry_indent = obj_indent + "  "

    new_entries = []
    for ln in body_lines:
        new_entries.append(entry_indent + ln.strip())

    insertion = "\n".join(new_entries) + "\n"
    result = base[:line_start] + insertion + base[line_start:]
    if result == base:
        return None
    return result
