"""
patch_replace — single-line replace, object-literal splice, fuzzy,
                signature replace.

Four heuristics that transform existing content in place:

  _try_single_line_replace      one-line body matched by identifier
                                to one line in base
  _try_object_literal_splice    body declares or reassigns an object
                                literal (`const X = {`, `X = {`,
                                `X.y = {`); preserves the base prefix
  _try_fuzzy_replace            replace-pair whose old_body is a
                                token-subset of a base window
  _try_signature_replace        body is a signature only (a `def`/
                                `function`/`class` header ending with
                                `:`); replace the matching signature
                                in base

MEDIUM-STABLE: these four encode "replace one thing with another".
Their contract is stable — the shape of the transform doesn't change
as new conversations surface.  Regex patterns for detecting what
*can* be replaced may evolve, but the mechanics of the transform
don't.

`_OBJECT_LITERAL_ASSIGNMENT_RE` is exported because `patch_dispatch`
and `patch_pairs` both consult it (for routing decisions and for the
pair-splice branch).

SIGNATURE-ONLY REPLACEMENT (2026-11-05)
=======================================
When the AI prints only the new signature of an existing method — a
common "add a parameter" edit — the block is a `def foo(...) -> ...:`
with no body statements.  Without this heuristic the block is either
(a) rejected by the Python completeness gate (a bare `def foo():` is
a SyntaxError), or (b) routed through `_apply_scoped` (which would
replace the whole function body with just a signature — data loss).

`_try_signature_replace` runs *before* the completeness gate in the
dispatcher, so a signature-only block never reaches it.  It finds the
matching signature in base (by name and by indentation) and replaces
just the signature lines, leaving the body untouched.

INDENTATION IS LOAD-BEARING (2026-10-28 fix)
============================================
The pre-fix `_try_signature_replace` called `body.strip()`, which
strips leading whitespace from the first line.  For an indented
method (`    def _paste_result(...)`), the extracted `base_indent`
was therefore `""`, and the signature search only matched
top-level definitions.  The signature-only block for
`_paste_result` in the comfyui.py conversation never matched; it
fell through to the completeness gate and failed.

The fix uses `body.strip("\n")` — strip only surrounding newlines,
not spaces.  Indentation of the signature line determines which
definition the heuristic targets.
"""

import re

from .patch_text import (
    _first_identifier,
    _line_token_jaccard,
    _find_matching_brace,
    _find_matching_paren,
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


# ---------------------------------------------------------------------------
#  Object-literal splice
# ---------------------------------------------------------------------------

def _try_object_literal_splice(base, body_stripped, varname):
    if not varname:
        return None
    name = re.escape(varname)
    base_re = re.compile(
        r'^([ \t]*(?:(?:export\s+)?(?:const|let|var)\s+)?'
        + name + r'\s*[=:])\s*\{',
        re.MULTILINE,
    )
    matches = list(base_re.finditer(base))
    if not matches:
        return None

    if len(matches) > 1:
        body_lines = [
            ln.strip() for ln in body_stripped.split("\n")
            if ln.strip() and len(ln.strip()) >= 8
        ]

        def _block_at(m):
            open_idx = m.end() - 1
            close_idx = _find_matching_brace(base, open_idx)
            if close_idx == -1:
                return None
            return base[m.start():close_idx + 1]

        scored = []
        for m in matches:
            block = _block_at(m)
            if block is None:
                continue
            score = sum(1 for ln in body_lines if ln in block)
            scored.append((score, m))

        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        if scored[0][0] == 0:
            return None
        base_m = scored[0][1]
    else:
        base_m = matches[0]

    # NEW (2026-10-28g): preserve the base's exact whitespace before
    # `{` rather than forcing a single space.
    #
    # Motivating case (comfyui.py, in the wild): base has `args={`
    # (no space); body has `args={...}`.  The old code produced
    # `args= {...}` (spurious space).  Style-wise wrong; worse,
    # `_max_contiguous_run` treats the modified `args= {` line as
    # different from base's `args={` line, so a subsequent re-run
    # sees the patch as still needed and re-applies it.
    #
    # Capturing `base[base_m.start():base_open]` gives exactly what
    # the source had between line start and `{`:
    #   · `        args=`          (source had no space)
    #   · `        const board = ` (source had a space)
    #   · `        args = `        (source had spaces on both sides)
    # In every case the result preserves the source's original style.
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

    prefix_through_open = base[base_m.start():base_open]
    result = (
        file_prefix
        + prefix_through_open
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


# ===========================================================================
#  Signature-only replace
# ===========================================================================
#
# A common AI edit shape: "here is the new signature of X, add the new
# parameter".  The block is a `def` header — possibly multi-line —
# ending in the closing `:` (or `-> type:`), with no body statements.

_SIG_FIRST_RE = re.compile(
    r'(?P<indent>[ \t]*)(?:async\s+)?(?:def|function|class)\s+'
    r'(?P<name>[A-Za-z_$][\w$]*)'
)


def _try_signature_replace(base, body):
    """Replace a matching signature in base with the body's signature.

    Fires only when the body is a signature-only fragment: the first
    line names a def/function/class, and the last line is the
    signature terminator (ends with `:`).

    FIX (2026-10-28): indentation is preserved.  `body.strip("\n")`
    keeps leading spaces, so `base_indent` correctly captures an
    indented method signature and the search only matches the
    matching-indent declaration in base.  `body.strip()` (the
    pre-fix call) discarded the indent and produced a spurious
    top-level match for indented methods.

    Returns the patched base, or None.
    """
    if not body:
        return None
    stripped = body.strip("\n")
    if not stripped.strip():
        return None

    body_lines = stripped.split("\n")
    first_line = body_lines[0]
    last_line = body_lines[-1].rstrip()

    first_m = _SIG_FIRST_RE.match(first_line)
    if not first_m:
        return None

    if not last_line.endswith(":"):
        return None

    name = first_m.group("name")
    base_indent = first_m.group("indent")

    sig_search = re.compile(
        r'(?m)^(' + re.escape(base_indent) + r')'
        r'(?:async\s+)?(?:def|function|class)\s+'
        + re.escape(name) + r'\b'
    )
    m2 = sig_search.search(base)
    if not m2:
        return None

    paren = base.find("(", m2.start())
    if paren == -1:
        return None
    close_paren = _find_matching_paren(base, paren)
    if close_paren == -1:
        return None

    tail = base[close_paren + 1:]
    tail_stripped = tail.lstrip()
    sig_end = close_paren + 1 + (len(tail) - len(tail_stripped))

    if tail_stripped.startswith("->"):
        colon = tail_stripped.find(":")
        if colon == -1:
            return None
        sig_end = (close_paren + 1) + (len(tail) - len(tail_stripped)) + colon
    elif not tail_stripped.startswith(":"):
        return None

    line_end = base.find("\n", sig_end)
    if line_end == -1:
        line_end = len(base)

    body_first_indent = len(body_lines[0]) - len(body_lines[0].lstrip(" \t"))
    normalized_lines = []
    for ln in body_lines:
        if not ln.strip():
            normalized_lines.append("")
            continue
        rel = len(ln) - len(ln.lstrip(" \t")) - body_first_indent
        normalized_lines.append(
            base_indent + " " * max(0, rel) + ln.lstrip(" \t"))
    normalized = "\n".join(normalized_lines)

    result = base[:m2.start()] + normalized + base[line_end:]
    return result if result != base else None
