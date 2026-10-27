"""
patch_heuristics — the "insert / replace / fuzzy" apply heuristics.

The bulk of the pipeline's growable logic lives here.  Each function
is one heuristic, documented with the shape of block it handles and
the failure modes it distinguishes from success.  Adding a new
heuristic means: write the function, add one call to
patch_dispatch._try_deterministic_patch_inner's chain, done.

The heuristics, in dispatch order:

  _try_single_line_replace      one-line body matched by identifier
                                to one line in base
  _try_object_literal_splice    body declares or reassigns an object
                                literal (`const X = {`, `X = {`,
                                `X.y = {`)
  _try_context_insert           2–30 lines, unique first-or-last-line
                                anchor
  _try_object_entry_insert      2–5 lines of `key: value,` entries
  _try_insert_after_anchor      body inserted after a unique line /
                                call / identifier-block matching a
                                backtick phrase in prose
  _try_fuzzy_replace            replace-pair whose old_body is a
                                token-subset of a base window
  _try_raw_diff_patch           body is a raw unified diff
  _delete_exact_block           body is a `[DEL]` target block
  _try_structural_append        last resort: no anchor, no scope —
                                infer position from AST or text
                                structure

Supporting predicates (`_patch_already_present`,
`_diff_already_applied`, `_deletion_already_applied`) check
"has this patch's intent already landed?" before a failure is
declared.  They are drift-tolerant by way of patch_text's
`_content_present_in_base` (see the DRIFT-FIX comments below).

Boundary completeness
=====================
Two insert heuristics place the body between two other lines:
`_try_insert_after_anchor` and `_try_context_insert`.  Different
rules apply:

  · `_try_insert_after_anchor` inserts after a *statement*.  The
    body must be a complete statement — last non-blank line ends
    with `;`, `}`, `)`, or is a comment.  A fragment ending with
    `,` is a continuation; landing it between two statements
    corrupts both.  This is the pg_comfy_client.py shape: the
    `body: JSON.stringify({...}),` fragment from the thought
    process was inserted between the signature's parameter list
    and the closing parenthesis, producing a genuinely broken
    function.  `_body_is_complete_at_boundary` catches it.

  · `_try_context_insert` inserts next to a *line*, which may be
    inside a comma-separated container (object literal, array,
    parameter list at the point of already-complete prior params).
    A fragment ending with `,` is a valid insertion here *when the
    anchor is itself a comma-continuation* — the container context
    is preserved by the anchor line.  `_context_insert_ok` encodes
    this: complete statement OR anchor ends with `,`.

No imports from patch_scope or patch_pairs — this module is the
bottom of the "apply strategy" subtree.
"""

import re
import os
import sys

from .core import (
    _is_deletion_instruction, _is_pure_deletion_diff_strict,
    _looks_like_diff_body, _strip_diff_prefixes,
)
from .patch_text import (
    _first_identifier, _line_token_jaccard,
    _find_matching_brace, _find_matching_paren,
    _reindent_body, _reindent_body_to_indent,
    _find_identifier_block_end_line,
    _looks_like_python_fragment,
    _content_present_in_base,
    _first_word,
)


# ===========================================================================
#  Shared constants
# ===========================================================================

_OBJECT_LITERAL_ASSIGNMENT_RE = re.compile(
    r'^(?P<prefix>'
    r'(?:export\s+)?'
    r'(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*='
    r'|'
    r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*='
    r')'
    r'\s*\{'
)


# ===========================================================================
#  Boundary-completeness predicates
# ===========================================================================
#
# An anchor-based insert places the body between two existing lines.
# The rules differ by heuristic — see the module docstring for the
# full reasoning.  Summary:
#
#   _body_is_complete_at_boundary(body)
#       Strict.  Body's last non-blank line must end with `;`, `}`,
#       `)`, or be a comment.  Used by `_try_insert_after_anchor`,
#       where the surrounding context is a statement stream and a
#       continuation fragment would corrupt both neighbours.
#
#   _context_insert_ok(anchor_line, body)
#       Relaxed.  Passes if the body is complete OR the anchor line
#       ends with `,`.  Used by `_try_context_insert`, where the
#       anchor line's comma is the container context that makes a
#       fragment valid.

def _body_is_complete_at_boundary(body):
    """True iff `body` can be inserted between two lines as a
    complete statement.

    Complete = last non-blank line ends with `;`, `}`, or `)`,
    OR the body is a single-line comment (`// ...` or `# ...`),
    OR the body is a standalone block comment (`/* ... */`).
    Everything else is either a continuation (ends with `,`,
    `+`, `*`, etc.), an opener (ends with `{` or `(`), or a
    fragment.
    """
    if not body or not body.strip():
        return False
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    first = lines[0].strip()
    last = lines[-1].strip()
    if last.startswith("//") or last.startswith("#"):
        return True
    if last.startswith("/*") and last.endswith("*/"):
        return True
    if last.endswith((";", "}", ")")):
        return True
    if last == "}":
        return True
    return False


def _context_insert_ok(anchor_line, body):
    """The body can be inserted adjacent to the anchor iff either:

    - the body is a complete statement (last non-blank line ends with
      `;`, `}`, `)`, or is a comment), OR
    - the anchor line ends with `,`, so the body is being appended
      to a comma-separated container (object literal, array,
      parameter list).

    The comma-anchor case covers the object-entry insert pattern:
    `key: value,` before `other: value,` is a valid addition to the
    object literal even though the added line itself ends with `,`.
    Without this branch, `inpaintFullCanvas: false,` before
    `inpaintPrompt: "",` would be refused because neither line is
    a "complete statement" by the strict predicate.
    """
    if _body_is_complete_at_boundary(body):
        return True
    if anchor_line.strip().endswith(","):
        return True
    return False


# ===========================================================================
#  Single-line replace
# ===========================================================================
#
# A body that is exactly one non-blank line and matches a line already
# in base by identifier + token-similarity, when the prose is not an
# add-instruction, is a *substitution* of that line, not an insertion.

_ONE_LINE_MIN_SIM = 0.35
_ONE_LINE_MIN_GAP = 0.20


def _try_single_line_replace(base, raw_body, context_before=""):
    """Replace exactly one base line with a one-line body.

    Fires only when:
      · the body has exactly one non-blank line;
      · the line is not a block opener/closer;
      · the prose does not signal an addition;
      · the line has an identifier that matches a base line's
        identifier;
      · the token-Jaccard similarity to that base line is
        unambiguous (best - second-best ≥ _ONE_LINE_MIN_GAP).
    """
    lines = [ln for ln in raw_body.split("\n") if ln.strip()]
    if len(lines) != 1:
        return None
    new_line = lines[0].rstrip()
    stripped = new_line.strip()
    if stripped.endswith("{") or stripped.startswith("}"):
        return None
    if _OBJECT_LITERAL_ASSIGNMENT_RE.match(stripped):
        return None

    lower = (context_before or "").lower()
    if any(v in lower for v in ("add ", "insert ", "append ", "prepend ")):
        return None

    new_ident = _first_identifier(stripped)
    if not new_ident:
        return None

    base_lines = base.split("\n")
    candidates = []
    for i, bl in enumerate(base_lines):
        if not bl.strip():
            continue
        cand_ident = _first_identifier(bl.strip())
        if cand_ident != new_ident:
            continue
        sim = _line_token_jaccard(bl.strip(), stripped)
        if sim >= _ONE_LINE_MIN_SIM:
            candidates.append((sim, i))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    best_sim, best_idx = candidates[0]

    if len(candidates) > 1 and (best_sim - candidates[1][0]) < _ONE_LINE_MIN_GAP:
        return None

    old_line = base_lines[best_idx]
    old_indent = old_line[:len(old_line) - len(old_line.lstrip())]
    replacement = old_indent + stripped

    result_lines = list(base_lines)
    result_lines[best_idx] = replacement
    result = "\n".join(result_lines)
    return result if result != base else None


# ===========================================================================
#  Object-literal splice
# ===========================================================================

def _try_object_literal_splice(base, body_stripped, varname):
    """Replace an object literal in place, preserving the base's
    leading prefix.

    Handles these base shapes:
      · `export const X = { ... }`
      · `const X = { ... }`
      · `let X = { ... }`
      · `var X = { ... }`
      · `X = { ... }` (assignment)
      · `a.b.c = { ... }` (dotted assignment)
      · `X: { ... }` (object-literal entry)

    The body must be one of the same shapes (plus `X = { ... }`
    assignment).  The result keeps the base's prefix (`export const`
    / `const` / `let` / `var`).
    """
    if not varname:
        return None
    name = re.escape(varname)
    base_re = re.compile(
        r'^([ \t]*(?:(?:export\s+)?(?:const|let|var)\s+)?'
        + name + r'\s*[=:])\s*\{',
        re.MULTILINE,
    )
    base_m = base_re.search(base)
    if not base_m:
        return None

    prefix_from_base = base_m.group(1)
    file_prefix = base[:base_m.start()]

    body_m = _OBJECT_LITERAL_ASSIGNMENT_RE.match(body_stripped)
    if not body_m:
        return None
    patch_open = body_stripped.index("{", body_m.start())
    patch_is_py = _looks_like_python_fragment(body_stripped)
    patch_close = _find_matching_brace(
        body_stripped, patch_open, is_python=patch_is_py)
    if patch_close < 0:
        return None
    body_obj = body_stripped[patch_open:patch_close + 1]

    base_open = base_m.end() - 1
    base_is_py = _looks_like_python_fragment(base[base_m.start():])
    base_close = _find_matching_brace(
        base, base_open, is_python=base_is_py)
    if base_close < 0:
        return None

    file_tail = base[base_close + 1:]
    ft_lstripped = file_tail.lstrip()
    if ft_lstripped.startswith(";"):
        file_tail = ft_lstripped[1:]
        base_had_semi = True
    else:
        base_had_semi = False

    body_had_semi = body_stripped.rstrip().endswith(";")
    sep = ";" if (base_had_semi or body_had_semi) else ""

    result = (
        file_prefix
        + prefix_from_base
        + " "
        + body_obj
        + sep
        + file_tail
    )
    return result if result != base else None


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


# ===========================================================================
#  Fuzzy replace-pair matching
# ===========================================================================

def _try_fuzzy_replace(base, old_body, new_body, threshold=0.75):
    """Similarity-based replace-pair fallback.

    Fires only when byte-exact and whitespace-normalized matching
    both fail.  Accepts a base window as a candidate only if, for
    every line pair, the base line is a strict token superset of the
    old_body line (or token-equivalent, meaning whitespace-only
    drift) AND the length ratio is at most 2x.
    """
    import re as _re
    from collections import Counter
    base_lines = base.splitlines()
    old_lines = old_body.splitlines()
    n = len(old_lines)
    if n == 0 or len(base_lines) < n:
        return None

    def _tokens(s):
        return Counter(_re.findall(
            r'''[A-Za-z_$][\w$]*|[0-9]+|"[^"]*"|'[^']*'|[^\s\w]''',
            s))

    def _line_sim(a, b):
        if a == b:
            return 1.0
        if not a or not b:
            return 0.0
        a_tok = _tokens(a)
        b_tok = _tokens(b)
        if a_tok == b_tok:
            return 0.95
        if not (a_tok - b_tok):
            la, lb = len(a), len(b)
            if lb < la or lb > la * 2:
                return 0.0
            return 0.9
        return 0.0

    scored = []
    for i in range(len(base_lines) - n + 1):
        window = base_lines[i:i+n]
        scores = []
        reject = False
        for a, b in zip(old_lines, window):
            a_s = a.strip()
            b_s = b.strip()
            s = _line_sim(a_s, b_s)
            if s == 0.0 and a_s and b_s:
                reject = True
                break
            scores.append(s)
        if reject or not scores:
            continue
        avg = sum(scores) / len(scores)
        scored.append((avg, i))

    if not scored:
        return None
    scored.sort(reverse=True)
    best_score, best_idx = scored[0]
    if best_score < threshold:
        return None
    if len(scored) > 1:
        second_score, second_idx = scored[1]
        if abs(second_idx - best_idx) <= n:
            if best_score - second_score < 0.1:
                return None
        elif best_score - second_score < 0.05:
            return None

    new_lines = new_body.splitlines()
    result_lines = base_lines[:best_idx] + new_lines + base_lines[best_idx+n:]
    trailing = "\n" if base.endswith("\n") else ""
    return "\n".join(result_lines) + trailing


# ===========================================================================
#  Deletion / diff / already-present predicates
# ===========================================================================

def _delete_exact_block(base, body):
    is_diff = _is_pure_deletion_diff_strict(body) or _looks_like_diff_body(body)
    stripped = _strip_diff_prefixes(body) if is_diff else body
    body_lines = [ln.rstrip() for ln in stripped.splitlines()]
    while body_lines and not body_lines[0]:
        body_lines.pop(0)
    while body_lines and not body_lines[-1]:
        body_lines.pop()
    if not body_lines:
        return None
    base_lines_keepends = base.splitlines(keepends=True)
    base_lines = [ln.rstrip("\n").rstrip() for ln in base_lines_keepends]
    n = len(body_lines)
    matches = []
    for i in range(len(base_lines) - n + 1):
        if base_lines[i:i+n] == body_lines:
            matches.append(i)
    if len(matches) != 1:
        return None
    start = matches[0]
    end = start + n
    new_lines = base_lines_keepends[:start] + base_lines_keepends[end:]
    new_text = "".join(new_lines)
    if new_text == base:
        return None
    return new_text


def _deletion_already_applied(body, base):
    stripped = _strip_diff_prefixes(body)
    body_lines = [ln.rstrip() for ln in stripped.splitlines() if ln.strip()]
    if not body_lines:
        return False
    base_nonempty = [ln.rstrip() for ln in base.splitlines() if ln.strip()]
    n = len(body_lines)
    for i in range(len(base_nonempty) - n + 1):
        if base_nonempty[i:i+n] == body_lines:
            return False
    return True


def _try_raw_diff_patch(base, body):
    diff_lines = body.splitlines()
    if not any(ln.startswith(("+", "-")) for ln in diff_lines):
        return None
    if any(ln.startswith(("+++", "---", "@@")) for ln in diff_lines):
        return None
    first_anchor = None
    for ln in diff_lines:
        if not ln:
            continue
        prefix = ln[:1]
        if prefix in (" ", "-"):
            first_anchor = ln[1:].rstrip()
            break
    if first_anchor is None:
        return None
    base_lines = base.splitlines(keepends=True)
    anchors = [
        idx for idx, ln in enumerate(base_lines)
        if ln.rstrip() == first_anchor
    ]
    if not anchors:
        return None
    for start in anchors:
        result = base_lines[:start]
        pos = start
        i = 0
        ok = True
        while i < len(diff_lines):
            line = diff_lines[i]
            i += 1
            if not line:
                line = " "
            prefix = line[:1]
            rest = line[1:]
            if prefix == " ":
                if pos >= len(base_lines) or (
                        base_lines[pos].rstrip() != rest.rstrip()):
                    ok = False
                    break
                result.append(base_lines[pos])
                pos += 1
            elif prefix == "-":
                if pos >= len(base_lines) or (
                        base_lines[pos].rstrip() != rest.rstrip()):
                    ok = False
                    break
                pos += 1
            elif prefix == "+":
                result.append(rest + "\n")
        if ok:
            result.extend(base_lines[pos:])
            new_text = "".join(result)
            if new_text == base:
                return None
            return new_text
    return None


def _diff_already_applied(body, base):
    if not _looks_like_diff_body(body):
        return False
    adds = []
    dels = []
    for ln in body.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            adds.append(ln[1:].rstrip())
        elif ln.startswith("-") and not ln.startswith("---"):
            dels.append(ln[1:].rstrip())
    base_lines_set = {ln.rstrip() for ln in base.splitlines()}
    for a in adds:
        if a and a not in base_lines_set:
            return False
    dels_nonempty = [d for d in dels if d.strip()]
    if dels_nonempty:
        base_lines_nonempty = [
            ln.rstrip() for ln in base.splitlines() if ln.strip()]
        n = len(dels_nonempty)
        for i in range(len(base_lines_nonempty) - n + 1):
            if base_lines_nonempty[i:i + n] == dels_nonempty:
                return False
    return True


def _patch_already_present(body, base):
    """True iff the patch's content is present in base.

    Exact-match fast path: a sliding window over base matches the
    body's line list exactly.

    DRIFT-FIX: after the exact check fails, run a token-equivalent
    membership check via `_content_present_in_base`.  The base may
    render the post-state slightly differently from how the patch
    wrote it (reordered conjuncts, reformatted expressions).  If 85%
    of the patch's lines are token-equivalent-present in base, the
    patch's intent is already reflected and the caller should mark
    this patch `already-present` rather than `failed`.
    """
    def norm(s):
        return [ln.strip() for ln in s.splitlines() if ln.strip()]
    body_lines = norm(body)
    if not body_lines:
        return True
    base_lines = norm(base)
    n = len(body_lines)
    if n <= len(base_lines):
        for i in range(len(base_lines) - n + 1):
            if base_lines[i:i+n] == body_lines:
                return True
    if _content_present_in_base(base, body, threshold=0.85):
        return True
    return False


# ===========================================================================
#  Structural append — text-only fallback when AST is unavailable
# ===========================================================================

_DECL_KEYWORDS   = frozenset({"const", "let", "var", "function",
                               "class", "def"})
_STMT_KEYWORDS   = frozenset({"if", "for", "while", "do", "try",
                               "throw", "return", "switch", "raise",
                               "with", "assert"})
_JS_LANGS        = frozenset({"javascript", "typescript", "tsx"})


def _infer_body_lang(body):
    """Best-effort language ID without tree-sitter."""
    text = body[:800]
    js = 0
    py = 0
    if "=>" in text:
        js += 2
    if "function " in text or "const " in text or "let " in text \
            or "var " in text:
        js += 1
    if text.lstrip().startswith(("if (", "for (", "while (", "try {")):
        js += 1
    if "document." in text or "window." in text or "board." in text:
        js += 1
    if "def " in text or text.lstrip().startswith(("class ", "@")):
        py += 2
    if "self." in text:
        py += 1
    if "import " in text and ("\n" in text):
        if "import {" not in text and "import *" not in text:
            py += 1
    if js > py and js >= 2:
        return "javascript"
    if py > js and py >= 2:
        return "python"
    return None


def _try_structural_append(base, body, path):
    """Insert `body` into `base` at a structurally-inferred point."""
    if not body or not body.strip():
        return None
    body_lines = body.rstrip().split("\n")
    first = ""
    for ln in body_lines:
        if ln.strip():
            first = ln.lstrip()
            break
    if not first:
        return None
    w = _first_word(first)
    is_decl = w in _DECL_KEYWORDS
    is_stmt = w in _STMT_KEYWORDS
    if not (is_decl or is_stmt):
        return None

    body_lang = _infer_body_lang(body)
    if body_lang is None:
        return None

    from .validate_output import _js_balance
    braces, parens, brackets, unclosed = _js_balance(body)
    if braces != 0 or parens != 0 or brackets != 0 or unclosed:
        print(f"  [structural] {path}: refusing to append "
              f"structurally-unbalanced block "
              f"(braces={braces:+d} parens={parens:+d} "
              f"brackets={brackets:+d})", file=sys.stderr)
        return None

    ext = os.path.splitext(path)[1].lower()
    from .ast_anchor import _LANG_BY_EXT
    container_lang = _LANG_BY_EXT.get(ext)
    if container_lang is None:
        return None

    if body_lang == container_lang:
        if is_decl:
            return _append_decl_after_last(base, body, body_lang)
        return _append_stmt_in_last_function(base, body, body_lang)

    if body_lang in _JS_LANGS and container_lang == "python":
        return _append_embedded_in_python_string(
            base, body, body_lang, is_decl)

    return None


def _find_matching_decl_block(content, block_first_line):
    m = re.match(
        r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(.+?)\s*;?\s*$',
        block_first_line)
    if not m:
        return None
    block_rhs = m.group(2)

    def abstract(s):
        s = re.sub(r'"[^"]*"', '""', s)
        s = re.sub(r"'[^']*'", "''", s)
        return s

    block_pattern = abstract(block_rhs)
    lines = content.split("\n")
    matches = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if line[0] in (" ", "\t"):
            continue
        lm = re.match(
            r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(.+?)\s*;?\s*$',
            line.strip())
        if not lm:
            continue
        existing_rhs = lm.group(2)
        if abstract(existing_rhs) != block_pattern:
            continue
        existing_ident = lm.group(1)
        end = _find_identifier_block_end_line(content, existing_ident)
        if end is not None:
            matches.append(end + 1)
    if not matches:
        return None
    return matches[-1]


def _find_last_top_level_js_decl_block_end(content):
    lines = content.split("\n")
    last_end = None
    for i, line in enumerate(lines):
        if not line.strip() or line[0] in (" ", "\t"):
            continue
        m = re.match(
            r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=',
            line.strip())
        if m:
            ident = m.group(1)
            end = _find_identifier_block_end_line(content, ident)
            if end is not None:
                last_end = end + 1
    return last_end


def _append_decl_after_last(content, body, lang):
    if lang not in _JS_LANGS:
        stripped = content.rstrip("\n")
        trailing = content[len(stripped):]
        return stripped + "\n\n" + body.rstrip() + trailing

    first_line = ""
    for ln in body.split("\n"):
        if ln.strip():
            first_line = ln.strip()
            break
    if not first_line:
        return None

    insert_line = _find_matching_decl_block(content, first_line)
    if insert_line is None:
        insert_line = _find_last_top_level_js_decl_block_end(content)
    if insert_line is None:
        stripped = content.rstrip("\n")
        trailing = content[len(stripped):]
        return stripped + "\n\n" + body.rstrip() + trailing

    lines = content.split("\n")
    result = "\n".join(
        lines[:insert_line] + [body.rstrip()] + lines[insert_line:])
    return result if result != content else None


def _find_last_top_level_js_function_close(content):
    lines = content.split("\n")
    starts = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or line[0] in (" ", "\t"):
            continue
        if re.match(
                r'(?:export\s+)?(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(',
                stripped):
            starts.append(i)
    if not starts:
        return None
    last_start = starts[-1]

    brace_line = last_start
    brace_col = lines[brace_line].find("{")
    while brace_col < 0 and brace_line < len(lines) - 1:
        brace_line += 1
        brace_col = lines[brace_line].find("{")
    if brace_col < 0:
        return None

    depth = 0
    for j in range(brace_line, len(lines)):
        start_col = brace_col if j == brace_line else 0
        for ch in lines[j][start_col:]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body_indent = "  "
                    for k in range(brace_line + 1, len(lines)):
                        if lines[k].strip():
                            indent = lines[k][:len(lines[k]) -
                                              len(lines[k].lstrip())]
                            body_indent = indent
                            break
                    return (j, body_indent)
    return None


def _append_stmt_in_last_function(content, body, lang):
    if lang not in _JS_LANGS:
        return None
    found = _find_last_top_level_js_function_close(content)
    if found is None:
        return None
    close_line, body_indent = found

    reindented = _reindent_body_to_indent(body, body_indent)
    lines = content.split("\n")
    result = "\n".join(
        lines[:close_line] + reindented.split("\n") + lines[close_line:])
    return result if result != content else None


def _append_embedded_in_python_string(base, body, body_lang, is_decl):
    from .ast_anchor import _TRIPLE_DOUBLE_RE, _TRIPLE_SINGLE_RE
    all_matches = list(_TRIPLE_DOUBLE_RE.finditer(base)) \
                  + list(_TRIPLE_SINGLE_RE.finditer(base))
    if not all_matches:
        return None
    all_matches.sort(key=lambda m: m.start())

    for m in reversed(all_matches):
        content = m.group(1)
        if not content or len(content) < 100:
            continue
        if is_decl:
            new_content = _append_decl_after_last(content, body, body_lang)
        else:
            new_content = _append_stmt_in_last_function(
                content, body, body_lang)
        if new_content is None or new_content == content:
            continue
        content_start = m.start(1)
        return (base[:content_start] + new_content
                + base[content_start + len(content):])
    return None
