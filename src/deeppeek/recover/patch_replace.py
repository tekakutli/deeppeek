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
                                structural match for a base window
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
top-level definitions.  The fix uses `body.strip("\n")` — strip only
surrounding newlines, not spaces.

STRUCTURAL SINGLE-LINE MATCHING (2026-11-05)
============================================
`_try_single_line_replace` gains two structural upgrades:

  · `_line_similarity_structurally` — parses both lines as statements
    in the destination language and scores via `statement_sig` when
    both parse.  Token Jaccard (order-insensitive, string-literal-
    naive) remains the fallback for cases the AST cannot judge:
    JS object properties (`value: false,` is a `labeled_statement`,
    not in `_JS_STATEMENT_TYPES`), Python dict entries inside
    braces, and short fragments that need context to parse.

  · Result-parse validation — after building the swap, `parse_source`
    on the full result.  If it fails, decline.  This moves the
    failure-mode signal from `skipped_structural_damage` (pass 6
    sanity gate fires, patch marked `deterministic → skipped`) to
    `failed` (this heuristic declines, next heuristic tries, or
    fail-closed with an honest "no heuristic matched" reason).

STRUCTURAL FUZZY-REPLACE (2026-11-05)
=====================================
`_try_fuzzy_replace` is called by `patch_pairs._try_replace_pair` as
the last resort after exact and whitespace-normalized matches fail:
the base has drifted but is recognizably the same code.  The
pre-fix implementation compared line-by-line with token Jaccard —
any line-alignment shift (blank line inserted, statement split or
joined, one line's tokens reordered) either rejected the whole match
or picked a wrong window.

The structural version:
  1. Parses `old_body` as a statement list (Python AST via
     `_parse_python_or_dedent`, or tree-sitter-javascript).
  2. Parses `base` the same way.
  3. Slides a window of len(old_stmts) over base_stmts, scoring each
     window by `statement_sig` per aligned statement.
  4. Fires when the best window beats `threshold` with a clear
     gap (0.10 for close-index windows, 0.05 for distant) over the
     runner-up.
  5. Splices `new_body` over the matched base window via
     `patch_bounds.splice_region`, which validates structurally.

Falls back to `_try_fuzzy_replace_lines` (the pre-2026-11-05
implementation, unchanged) when `old_body` cannot be parsed as
statements: JS object properties, HTML fragments, prose-like YAML,
etc.  The flat-line path is the only signal available for those.

The same `path` parameter drives grammar selection here as in
`_try_single_line_replace`; both callers pass it through so a `.py`
fuzzy-replace gets Python AST scoring, a `.js` fuzzy-replace gets
tree-sitter, and a `.html` fuzzy-replace gets the line-based
fallback.

STRUCTURAL OBJECT-LITERAL DISAMBIGUATION (2026-11-05)
=====================================================
When `_try_object_literal_splice` finds more than one `const name =
{` in base, the pre-fix version scored each candidate by counting
how many body *lines* appear verbatim in the block.  A base block
with a comment interleaved (`key: /* x */ value,`), or extra
internal whitespace (`key:    value,`), breaks the line-equality
match even when the keys are identical.

The structural version extracts the set of dict/object keys from the
body's literal and from each candidate block's literal, and scores
by Jaccard overlap.  Key extraction is brace-scan + regex — cheap,
language-agnostic (Python dicts and JS objects use the same
`key: value` shape for our purposes), and reformatting-invariant.

LINE-SIG CACHING (2026-11-05)
=============================
`_line_similarity_structurally` calls `parse_source(a, "<line>")`
and `parse_source(b, "<line>")` for every candidate line compared
against a new-line body.  Each call parses a one-line string —
unique per call, so `parse_source`'s memoization never hits.

Two `lru_cache`-decorated helpers (`_python_line_sig`,
`_js_line_sig`) cache the *full result* — parse + `statement_sig`
extraction — keyed on the line string.  Identical lines (boilerplate
`return;`, `}` closers, repeated error handlers) hit the cache; the
per-line cost drops to a dict lookup.

`_try_single_line_replace` also caps AST-sim at
`_AST_SIM_CANDIDATE_MAX = 8`.  Above that, it falls back to
`_line_token_jaccard` (cheap, no parsing) — a file with dozens of
lines sharing the body's first identifier no longer pays N× parse
costs to discriminate.
"""
from __future__ import annotations

import ast
import re
import textwrap
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ._ast import parse_source
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
#  Line-level structural similarity — cached sigs  (2026-11-05)
# ===========================================================================

@lru_cache(maxsize=2048)
def _python_line_sig(line: str):
    """Parse one line as Python and return its `statement_sig`, or
    None.

    Cached on the raw line string.  See module docstring,
    LINE-SIG CACHING.
    """
    try:
        tree = parse_source(line, filename="<line>")
    except SyntaxError:
        return None
    if len(tree.body) != 1:
        return None
    from .statement_sig import _python_sig
    return _python_sig(tree.body[0])


@lru_cache(maxsize=2048)
def _js_line_sig(line: str):
    """Parse one line as JS via tree-sitter and return its
    `statement_sig`, or None.

    Returns None when tree-sitter-javascript is not installed, when
    the line doesn't parse as exactly one child node, or when
    `_tree_sitter_sig` declines.

    Cached on the raw line string.  See module docstring,
    LINE-SIG CACHING.
    """
    try:
        from .ast_anchor import _get_parser
        parser = _get_parser("javascript")
    except Exception:
        return None
    if parser is None:
        return None
    try:
        tree = parser.parse(line.encode("utf-8"))
    except Exception:
        return None
    if tree.root_node.has_error or len(tree.root_node.children) != 1:
        return None
    from .statement_sig import _tree_sitter_sig
    return _tree_sitter_sig(tree.root_node.children[0], "javascript")


def _line_similarity_structurally(a: str, b: str, path: str) -> float:
    """Structural similarity for two single lines.

    When both lines parse as a single statement in the destination
    language, score by `statement_sig`.  Otherwise fall back to
    `_line_token_jaccard`.

    The AST path gives order-sensitivity that token Jaccard lacks:
    `foo(a, b)` and `foo(b, a)` have identical token sets but
    different call semantics; `x = compute(a) + compute(b)` and
    `x = compute(b) + compute(a)` likewise.  Neither ordering change
    is visible to `_line_token_jaccard` but both are visible to
    `statement_sig`.

    The fallback covers the cases the AST cannot judge: JS object
    properties (`value: false,` parses as a labeled statement in
    JS, not a call/return/assign), Python dict entries inside
    braces (`"key": value,` doesn't parse standalone), and short
    fragments that need surrounding context.  Those are exactly the
    common `_try_single_line_replace` use cases, so the fallback is
    load-bearing, not vestigial.
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    ext = Path(path).suffix.lower() if path else ""
    from .statement_sig import score

    if ext in (".py", ".pyi"):
        sa = _python_line_sig(a)
        sb = _python_line_sig(b)
        if sa is not None and sb is not None:
            return score(sa, sb)

    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        sa = _js_line_sig(a)
        sb = _js_line_sig(b)
        if sa is not None and sb is not None:
            return score(sa, sb)

    return _line_token_jaccard(a, b)


# ===========================================================================
#  Single-line replace
# ===========================================================================
#
# A body that is exactly one non-blank line and matches a line already
# in base by identifier + structural similarity, when the prose is not
# an add-instruction, is a *substitution* of that line, not an
# insertion.

_ONE_LINE_MIN_SIM = 0.35
_ONE_LINE_MIN_GAP = 0.20

# Above this many candidate lines, skip the AST-similarity path and
# use token Jaccard only.  See module docstring, LINE-SIG CACHING.
_AST_SIM_CANDIDATE_MAX = 8


def _try_single_line_replace(base, raw_body, context_before="", path=""):
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

    # Collect candidate indices first (cheap identifier match).
    # Above `_AST_SIM_CANDIDATE_MAX`, skip the per-line AST parse
    # and use token Jaccard only — a file with dozens of lines
    # sharing the body's first identifier would otherwise pay N×
    # parse costs.  See module docstring, LINE-SIG CACHING.
    candidate_idxs = []
    for i, bl in enumerate(base_lines):
        if not bl.strip():
            continue
        cand_ident = _first_identifier(bl.strip())
        if cand_ident != new_ident:
            continue
        candidate_idxs.append(i)

    use_ast = len(candidate_idxs) <= _AST_SIM_CANDIDATE_MAX
    candidates = []
    for i in candidate_idxs:
        bl = base_lines[i].strip()
        if use_ast:
            sim = _line_similarity_structurally(bl, stripped, path)
        else:
            sim = _line_token_jaccard(bl, stripped)
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
    if result == base:
        return None

    # Refuse if the swap would make the file unparseable.  The pass-6
    # sanity gate would catch this eventually, but declining here
    # means the failure is a `failed` patch with an honest reason
    # instead of a `deterministic → skipped_structural_damage`
    # demotion that masks the real cause.
    #
    # `structural_valid` is language-aware (ast for .py, tree-sitter
    # for .js/.ts, tree-sitter-html for .html), unlike `parse_source`
    # which is Python-only.  Using `parse_source` here regressed
    # every `.js` single-line replace — `ast.parse` on JS content
    # always fails, so the swap was never applied (fixture 34,
    # `attribute_sanity_failure finds culprit`).
    if path:
        from .patch_bounds import structural_valid
        if not structural_valid(result, path):
            return None

    return result


# ===========================================================================
#  Object-literal splice
# ===========================================================================

_OBJECT_KEY_RE = re.compile(
    r'["\']?([A-Za-z_$][\w$]*)["\']?\s*:')


def _extract_object_keys(text: str) -> set:
    """Extract the string/identifier keys of the first dict/object
    literal in `text`, or empty set.

    Brace-scan to find the first `{ ... }` span (respecting nested
    braces), then regex the key:value shape inside.  Language-agnostic
    for the two cases that matter here — Python dicts and JS objects
    use the same `key: value` surface syntax even though the AST
    underneath differs.

    Returns empty set when no balanced brace pair is found or when
    the body has no `key: value` pairs.
    """
    open_idx = text.find("{")
    if open_idx < 0:
        return set()
    depth = 0
    close_idx = -1
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx < 0:
        return set()
    body = text[open_idx:close_idx + 1]
    return set(_OBJECT_KEY_RE.findall(body))


def _try_object_literal_splice(base, body_stripped, varname, path=""):
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
        # Structural disambiguation: score candidates by dict-key
        # Jaccard against the body's dict keys.  Pre-fix version
        # scored by verbatim line presence, which a comment
        # interleaved in base (or extra whitespace inside a value)
        # defeated even when the keys matched exactly.
        body_keys = _extract_object_keys(body_stripped)

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
            block_keys = _extract_object_keys(block)
            if not body_keys or not block_keys:
                # No key signal — fall back to the verbatim line match
                # so tiny objects (0-key, 1-key) don't fail-closed.
                body_lines = [
                    ln.strip() for ln in body_stripped.split("\n")
                    if ln.strip() and len(ln.strip()) >= 8
                ]
                score = sum(1 for ln in body_lines if ln in block)
                scored.append((score, m))
                continue
            jaccard = (
                len(body_keys & block_keys) / len(body_keys | block_keys)
            )
            scored.append((jaccard, m))

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

_JS_STATEMENT_NODE_TYPES = frozenset({
    "return_statement",
    "expression_statement",
    "lexical_declaration",
    "variable_declaration",
    "if_statement",
    "for_statement",
    "while_statement",
    "try_statement",
    "function_declaration",
    "class_declaration",
})


def _parse_statement_list(text: str, path: str) -> list:
    """Parse `text` as a statement list in the destination language.

    Returns `[]` on unparseable input, on empty input, or when the
    destination extension has no parser.  Dedent fallback for
    fragment bodies (e.g. an indented `try:/except:` block).

    Python statements carry `.lineno` / `.end_lineno` (1-based
    inclusive).  Tree-sitter statements carry `.start_point` /
    `.end_point` (0-based, half-open on the col).  Callers convert
    to line ranges per language.
    """
    if not text or not text.strip():
        return []
    ext = Path(path).suffix.lower() if path else ""

    if ext in (".py", ".pyi"):
        candidates = [text]
        dedented = textwrap.dedent(text)
        if dedented != text:
            candidates.append(dedented)
        for cand in candidates:
            try:
                tree = parse_source(cand, filename=path or "<body>")
            except SyntaxError:
                continue
            return [n for n in tree.body
                    if hasattr(n, "lineno")
                    and hasattr(n, "end_lineno")]
        return []

    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        try:
            from .ast_anchor import _get_parser
            parser = _get_parser("javascript")
        except Exception:
            parser = None
        if parser is None:
            return []
        try:
            tree = parser.parse(text.encode("utf-8"))
        except Exception:
            return []
        if tree is None or tree.root_node.has_error:
            return []
        return [c for c in tree.root_node.children
                if c.type in _JS_STATEMENT_NODE_TYPES]

    return []


def _stmt_signature(stmt, path: str):
    """`statement_sig`-style signature for a parsed statement, or None."""
    ext = Path(path).suffix.lower() if path else ""
    try:
        if ext in (".py", ".pyi"):
            from .statement_sig import _python_sig
            return _python_sig(stmt)
        if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
            from .statement_sig import _tree_sitter_sig
            return _tree_sitter_sig(stmt, "javascript")
    except ImportError:
        return None
    return None


def _stmt_line_range(stmt, path: str) -> tuple:
    """Return `(start_line_0based, end_line_exclusive)` for `stmt`.

    Python: `.lineno` is 1-based inclusive; `.end_lineno` is 1-based
    inclusive; convert to 0-based [start, end_exclusive).

    Tree-sitter: `.start_point[0]` is 0-based; `.end_point[0]` is
    0-based and points to the last line the node touches; add 1 for
    exclusivity.  A node whose `.end_point[1]` is 0 but occupies
    nothing on the last line still ends on that line; adding 1 is
    correct because `splice_region` replaces lines `[start, end)`.
    """
    ext = Path(path).suffix.lower() if path else ""
    if ext in (".py", ".pyi"):
        return stmt.lineno - 1, stmt.end_lineno
    # tree-sitter
    return stmt.start_point[0], stmt.end_point[0] + 1


def _try_fuzzy_replace_structural(base, old_body, new_body, path,
                                   threshold) -> Optional[str]:
    """Structural statement-sig window match.  See module docstring,
    STRUCTURAL FUZZY-REPLACE.
    """
    old_stmts = _parse_statement_list(old_body, path)
    if not old_stmts:
        return None
    base_stmts = _parse_statement_list(base, path)
    if len(base_stmts) < len(old_stmts):
        return None

    n = len(old_stmts)
    old_sigs = [_stmt_signature(s, path) for s in old_stmts]
    base_sigs = [_stmt_signature(s, path) for s in base_stmts]
    if any(s is None for s in old_sigs):
        return None

    from .statement_sig import score as sig_score

    scored = []
    for i in range(len(base_stmts) - n + 1):
        window = base_sigs[i:i + n]
        if any(s is None for s in window):
            continue
        total = sum(sig_score(o, w) for o, w in zip(old_sigs, window))
        scored.append((total / n, i))

    if not scored:
        return None
    scored.sort(reverse=True)
    best_score, best_idx = scored[0]
    if best_score < threshold:
        return None
    if len(scored) > 1:
        second_score, second_idx = scored[1]
        if abs(second_idx - best_idx) <= n:
            if best_score - second_score < 0.10:
                return None
        elif best_score - second_score < 0.05:
            return None

    start_line, _ = _stmt_line_range(base_stmts[best_idx], path)
    _, end_line = _stmt_line_range(base_stmts[best_idx + n - 1], path)

    from .patch_bounds import splice_region
    out = splice_region(
        base, start_line, end_line,
        new_body.split("\n"), path)
    if out.status == "noop":
        return base
    if out.status == "changed":
        return out.content
    return None


def _try_fuzzy_replace_lines(base, old_body, new_body, threshold=0.75):
    """Line-level fuzzy replace.  Pre-2026-11-05 implementation, kept
    as the fallback for bodies the structural matcher cannot judge.
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
            r"""[A-Za-z_$][\w$]*|[0-9]+|"[^"]*"|'[^']*'|[^\s\w]""", s))

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


def _try_fuzzy_replace(base, old_body, new_body, path="", threshold=0.75):
    """Match `old_body` against a window of `base` and replace it with
    `new_body`.

    Structural path first (`_try_fuzzy_replace_structural`); line-level
    fallback (`_try_fuzzy_replace_lines`) when the structural matcher
    cannot parse old_body or base.  See the module docstring,
    STRUCTURAL FUZZY-REPLACE.
    """
    if not old_body or not old_body.strip():
        return None
    if not new_body or not new_body.strip():
        return None

    structural = _try_fuzzy_replace_structural(
        base, old_body, new_body, path, threshold)
    if structural is not None:
        return structural

    return _try_fuzzy_replace_lines(
        base, old_body, new_body, threshold=threshold)


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

    Unchanged in the 2026-11-05 structural pass: the regex gate
    ("does this look like a signature header?") is cheap and
    accurate; `ast.parse` of a bare `def foo(...):` raises
    SyntaxError, so AST is strictly worse for this predicate.  The
    splice itself already uses `_find_matching_paren` which is
    char-scan-based and correct for nested parens.

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
