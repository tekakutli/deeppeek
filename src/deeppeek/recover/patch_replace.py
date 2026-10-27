"""
patch_replace — single-line replace, object-literal splice, fuzzy.

Three heuristics that transform existing content in place:

  _try_single_line_replace      one-line body matched by identifier
                                to one line in base
  _try_object_literal_splice    body declares or reassigns an object
                                literal (`const X = {`, `X = {`,
                                `X.y = {`); preserves the base prefix
  _try_fuzzy_replace            replace-pair whose old_body is a
                                token-subset of a base window

MEDIUM-STABLE: these three encode "replace one thing with another".
Their contract is stable — the shape of the transform doesn't change
as new conversations surface.  Regex patterns for detecting what
*can* be replaced may evolve, but the mechanics of the transform
don't.

`_OBJECT_LITERAL_ASSIGNMENT_RE` is exported because `patch_dispatch`
and `patch_pairs` both consult it (for routing decisions and for the
pair-splice branch).
"""

import re

from .patch_text import (
    _first_identifier,
    _line_token_jaccard,
    _find_matching_brace,
    _looks_like_python_fragment,
)


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
            r"""[A-Za-z_$][\w$]*|[0-9]+|"[^"]*"|'[^']*'|[^\s\w]""",
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
