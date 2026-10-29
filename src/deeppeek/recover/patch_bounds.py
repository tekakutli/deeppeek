"""Boundary adjustment + structural validation for patch splices.

The three heuristics that do a region *replace* (`_try_replace_intent`,
`_try_region_replace`, `_apply_scoped_single`) all pick a `[start, end)`
line window by heuristics, splice the body in, and return whatever the
splice produced. Two failure modes are common:

  1. Off-by-one boundaries.  `end` lands mid-statement (before the
     closing `}` or `);`), so the old statement's tail survives as an
     orphan after the new body.

  2. Line-based boundaries without structural awareness.  The correct
     replacement region is the span between two AST statements; a
     line-based similarity search has no way to know that.

Both are addressable at the splice site without case-specific rules:

  · `structural_valid(content, path)` — parse-check `content`, including
    embedded JS/HTML inside Python raw-string literals. This is the
    missing gate that let pass-8 catch broken embedded JS one pass late.

  · `try_ast_snapped_boundaries(...)` — use `ast` (`.py`) or tree-sitter
    (`.js`/`.ts`/etc., and the dominant embedded JS string in a `.py`)
    to snap `[start, end)` outward to complete enclosing statements.

  · `try_boundary_pairs(...)` — expand/contract by ±1..N lines; cheap
    fallback when the AST snap can't help.

  · `splice_region(...)` — do the splice, validate, return `None` on
    parse error or extreme size loss.

Each heuristic wires them in identically: build a candidate list from
the AST snap + the pair sweep, try each, return the first valid one.
"""
from pathlib import Path
from typing import Optional


# ── structural validation ──────────────────────────────────────────

def structural_valid(content: str, path: str) -> bool:
    """True iff `content` parses structurally (incl. embedded JS/HTML).

    Empty or unrecognised `path` — which happens when callers invoke
    heuristics directly without a record path, as the test suite does
    — falls back to a generic parse-then-balance check.  Without this,
    the structural gate silently accepts broken content for empty-path
    calls, defeating the AST-snap that's supposed to reject off-by-one
    splices.
    """
    ext = Path(path).suffix.lower() if path else ""
    if ext == ".py":
        return _py_valid(content, path)
    if ext in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
        return _js_valid(content, path, ext)
    if ext == ".html":
        return _html_valid(content, path)
    # Unknown / empty extension: try Python parse first (most content
    # is Python or Python-containing), then JS balance.  A `.py` snippet
    # with apostrophes in `#` comments would pass `ast.parse` but fail
    # `_js_balance`; the reverse is true for a JS snippet containing
    # `#`, but that's rare.
    import ast
    import textwrap
    try:
        ast.parse(textwrap.dedent(content))
        return True
    except SyntaxError:
        pass
    from .validate_output import _js_balance
    b, p, k, u = _js_balance(content)
    return (b, p, k, u) == (0, 0, 0, 0)


def _py_valid(content: str, path: str) -> bool:
    import ast
    try:
        ast.parse(content)
    except SyntaxError:
        return False
    from .validate_output import (
        _iter_triple_quoted, _js_structural_check,
        _html_structural_check, _looks_like_js, _looks_like_html,
    )
    for offset, block in _iter_triple_quoted(content):
        lineno = content.count("\n", 0, offset) + 1
        label = f"{path} (embedded at line {lineno})"
        if _looks_like_html(block):
            if _html_structural_check(block, label) is not None:
                return False
        elif _looks_like_js(block):
            if _js_structural_check(
                    block, label, parse_lang="javascript") is not None:
                return False
    return True


def _js_valid(content: str, path: str, ext: str) -> bool:
    from .validate_output import _js_structural_check, _js_lang_for_ext
    return _js_structural_check(
        content, path, parse_lang=_js_lang_for_ext(ext)) is None


def _html_valid(content: str, path: str) -> bool:
    from .validate_output import _html_structural_check
    return _html_structural_check(content, path) is None


# ── AST-snapped boundaries ─────────────────────────────────────────

def try_ast_snapped_boundaries(content, path, start, end):
    """Snap `[start, end)` outward to enclosing statement boundaries.

    Returns `(new_start, new_end)` or `None`. For `.py` also considers
    the top-level statements of any dominant embedded JS string, so a
    `.py`-with-playground-JS case snaps to the correct JS nodes rather
    than the (irrelevant) Python wrapper.
    """
    ext = Path(path).suffix.lower() if path else ""
    stmt_ranges = []
    if ext == ".py":
        stmt_ranges.extend(_python_top_level_stmt_ranges(content))
        stmt_ranges.extend(_embedded_js_top_level_stmt_ranges(content))
    elif ext in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
        stmt_ranges = _js_top_level_stmt_ranges(content, ext)
    if not stmt_ranges:
        return None
    overlapping = [(s, e) for s, e in stmt_ranges
                   if s < end and e > start]
    if not overlapping:
        return None
    new_start = min(s for s, _ in overlapping)
    new_end = max(e for _, e in overlapping)
    if new_start == start and new_end == end:
        return None
    return new_start, new_end


def _python_top_level_stmt_ranges(content):
    import ast
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if getattr(node, "lineno", None) is None:
            continue
        if getattr(node, "end_lineno", None) is None:
            continue
        # ast lineno/end_lineno are 1-based inclusive; convert to
        # 0-based [start, end_exclusive).
        out.append((node.lineno - 1, node.end_lineno))
    return out


def _js_top_level_stmt_ranges(content, ext):
    from .ast_anchor import _get_parser, _LANG_BY_EXT
    lang = _LANG_BY_EXT.get(ext)
    if lang is None:
        return []
    parser = _get_parser(lang)
    if parser is None:
        return []
    try:
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return []
    if tree.root_node.has_error:
        return []
    return _tree_top_level_ranges(tree.root_node, offset=0)


def _embedded_js_top_level_stmt_ranges(content):
    from .ast_anchor import (
        _get_parser, _TRIPLE_DOUBLE_RE, _TRIPLE_SINGLE_RE,
    )
    parser = _get_parser("javascript")
    if parser is None:
        return []
    out = []
    matches = []
    for regex in (_TRIPLE_DOUBLE_RE, _TRIPLE_SINGLE_RE):
        for m in regex.finditer(content):
            content_start = m.start(1)
            string_content = m.group(1)
            if len(string_content) < 100:
                continue
            matches.append((content_start, string_content))
    for content_start, string_content in matches:
        try:
            tree = parser.parse(string_content.encode("utf-8"))
        except Exception:
            continue
        if tree.root_node.has_error:
            continue
        # The JS content begins on the same file line as the opening
        # `r"""`; `content_start_line` is the 0-based file line index
        # of that opening quote. JS line 0 is the empty line right
        # after it, which is still file line `content_start_line`.
        content_start_line = content.count("\n", 0, content_start)
        out.extend(_tree_top_level_ranges(
            tree.root_node, offset=content_start_line))
    return out


def _tree_top_level_ranges(root_node, offset):
    out = []
    for child in root_node.children:
        sp = child.start_point
        ep = child.end_point
        file_start = offset + sp[0]
        # tree-sitter end_point is (line, col) exclusive.  The node's
        # last touched line is ep[0] if ep[1] > 0, else ep[0] - 1.
        file_end = offset + ep[0] + (1 if ep[1] > 0 else 0)
        if file_end > file_start:
            out.append((file_start, file_end))
    return out


# ── expansion / contraction candidates ─────────────────────────────

def try_boundary_pairs(start, end, total, max_delta=3):
    """Yield `(start, end)` candidates: original, then ±delta.

    Expansions yielded before contractions — cutting a statement
    short is more likely to leave an orphan than including a
    neighbouring one, so the pipeline prefers expansion when both
    would produce valid content.
    """
    seen = set()

    def _emit(s, e):
        if 0 <= s < e <= total and (s, e) not in seen:
            seen.add((s, e))
            return True
        return False

    if _emit(start, end):
        yield start, end
    for d in range(1, max_delta + 1):
        for (s, e) in ((start - d, end + d),
                       (start - d, end),
                       (start, end + d),
                       (start + d, end),
                       (start, end - d)):
            if _emit(s, e):
                yield s, e


# ── splice + validate ──────────────────────────────────────────────

def splice_region(base, start, end, body_lines, path,
                  min_fraction=0.5) -> Optional[str]:
    """Splice base[start:end] with body_lines, validate, return or None."""
    base_lines = base.splitlines()
    if start < 0 or end > len(base_lines) or start >= end:
        return None
    new_lines = base_lines[:start] + list(body_lines) + base_lines[end:]
    if len(new_lines) < len(base_lines) * min_fraction:
        return None
    result = "\n".join(new_lines)
    if base.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    if result == base:
        return None
    if not structural_valid(result, path):
        return None
    return result
