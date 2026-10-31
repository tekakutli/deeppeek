"""
patch_statement_replace — place a single-statement body over one
base statement matched structurally.

Motivating shape (the general one, not any one project): a body is
one statement — a modified version of one statement in base — and
the two share no unique anchor line.  Line-level similarity scores
them by token overlap and can pick the wrong statement; a
structurally-scored match picks the right one.

Fires when:
  · body parses as exactly one statement in the destination's
    language;
  · that statement has an identifying signature (see statement_sig);
  · base has at least one statement with signature score > 0.55;
  · the best candidate beats the runner-up by >= 0.10 (fail-closed
    on near-ties);
  · the splice parses cleanly in the destination language.

`_MIN_GAP = 0.10` matches the reasoning in `patch_dispatch.py`'s
STRUCTURAL STATEMENT REPLACE PRE-CHECK section: the motivating
inpaint_server.py shape produces a target-vs-runner-up gap of
exactly 0.15, and floating-point representation of the score
difference is `0.14999…`.  A threshold of 0.15 would reject the
intended match on a boundary case; 0.10 accepts it with margin
while still rejecting genuinely ambiguous shapes (which produce
gaps below 0.05).

Insertion point: the dispatch chain, after `_try_multi_statement_apply`
and BEFORE `_try_region_replace`.  The structural match is semantic;
the line-based region-replace is a heuristic of last resort.  Running
the semantic matcher first means a body that *is* a modified
statement lands where it belongs.

Cross-language: Python via `ast`, JS/TS via tree-sitter (when
installed).  When the grammar is unavailable, the matcher returns
None and the dispatcher falls through — same graceful-degradation
contract as `ast_anchor`.

ALREADY-APPLIED NO-OP (2026-11-06)
==================================
`_splice()` returns the spliced content, or None on structural
failure.  The pre-fix callers treated *any* result equal to `base`
as a failure (`if result is None or result == base: return None`).
That conflates two semantically different outcomes:

  · "the splice is invalid — the candidate does not parse" — this
    is a fail-closed rejection, and the caller should try the next
    statement or heuristic.

  · "the splice produced the base — the base already equals the
    body" — this is "already applied", and the caller should return
    `base` so `_apply_pairs` / the already-present pre-check records
    it as a no-op.

When the matched base statement already equals the body's statement
(idempotent re-run of a conversation, or a body that happens to
restate the base), the pre-fix version recorded the patch as
`failed`, dropping the record to LOW trust on a logically-succeeded
patch.

The callers now return `base` on no-op and `None` only on `_splice`
failure or `_valid_python` / `_valid_ts` rejection.
"""
from __future__ import annotations
import ast
import textwrap
from pathlib import Path
from typing import Optional

from .statement_sig import extract as extract_sig, score as sig_score
from .ast_anchor import _get_parser, _LANG_BY_EXT
from ._ast import parse_source


_MIN_SCORE = 0.55
_MIN_GAP = 0.10


def _iter_python_stmts(tree):
    """Yield (lineno-1, end_lineno, stmt_node) for top-level statements
    AND every statement inside a function/class body (two levels deep
    from module).  Nested-in-expression lambdas/comprehensions are not
    statements and are skipped.
    """
    def _walk(node):
        for child in ast.iter_child_nodes(node):
            if hasattr(child, "lineno"):
                yield (child.lineno - 1, child.end_lineno, child)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.If, ast.Try,
                                  ast.With, ast.For, ast.While)):
                yield from _walk(child)
    yield from _walk(tree)


def _iter_ts_stmts(tree):
    """Yield (start_row, end_row_exclusive, node) for tree-sitter
    statements."""
    def _walk(node):
        for child in node.children:
            if child.type in {
                "return_statement", "expression_statement",
                "lexical_declaration", "variable_declaration",
                "if_statement", "for_statement", "while_statement",
                "try_statement", "function_declaration",
                "class_declaration",
            }:
                yield (child.start_point[0],
                       child.end_point[0] + (1 if child.end_point[1] else 0),
                       child)
            yield from _walk(child)
    yield from _walk(tree.root_node)


def _reindent_to(body_lines: list, target_indent: str) -> list:
    if not body_lines:
        return body_lines
    first = next((ln for ln in body_lines if ln.strip()), "")
    first_indent = first[:len(first) - len(first.lstrip())]
    out = []
    for ln in body_lines:
        if not ln.strip():
            out.append(ln)
            continue
        if ln.startswith(first_indent):
            out.append(target_indent + ln[len(first_indent):])
        else:
            out.append(target_indent + ln.lstrip())
    return out


def _splice(base_lines: list, start: int, end: int,
            body_lines: list) -> Optional[str]:
    if start < 0 or end > len(base_lines) or start >= end:
        return None
    result_lines = base_lines[:start] + body_lines + base_lines[end:]
    result = "\n".join(result_lines)
    if base_lines and result and not result.endswith("\n"):
        result += "\n"
    return result


def _valid_python(content: str) -> bool:
    try:
        parse_source(content)
    except SyntaxError:
        return False
    return True


def _valid_ts(content: str, lang: str) -> bool:
    parser = _get_parser(lang)
    if parser is None:
        return False
    try:
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return False
    return not tree.root_node.has_error


def try_statement_replace(base: str, body: str,
                          path: str = "") -> Optional[str]:
    """Structural replace of one statement.  Returns new content or
    None.  Never raises.

    Returns `base` (the unchanged input) on a no-op splice — the
    matched base statement already equals the body.  Callers
    distinguish "already present" from "invalid" by comparing the
    result against `base`.
    """
    if not base or not body or not path:
        return None

    ext = Path(path).suffix.lower()
    if ext == ".py":
        return _try_python(base, body, path)
    lang = _LANG_BY_EXT.get(ext)
    if lang is not None:
        return _try_ts(base, body, lang, path)
    return None


def _try_python(base, body, path):
    try:
        body_tree = parse_source(textwrap.dedent(body))
    except SyntaxError:
        return None
    if len(body_tree.body) != 1:
        return None
    body_stmt = body_tree.body[0]
    body_sig = extract_sig(body_stmt, path)
    if body_sig is None:
        return None

    try:
        base_tree = parse_source(base)
    except SyntaxError:
        return None

    candidates = []
    for start, end, node in _iter_python_stmts(base_tree):
        s = sig_score(body_sig, extract_sig(node, path))
        if s > 0:
            candidates.append((s, start, end))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    if candidates[0][0] < _MIN_SCORE:
        return None
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < _MIN_GAP:
        return None

    _, start, end = candidates[0]
    base_lines = base.splitlines()
    first_line = base_lines[start]
    indent = first_line[:len(first_line) - len(first_line.lstrip())]

    body_lines = _reindent_to(body.splitlines(), indent)
    result = _splice(base_lines, start, end, body_lines)
    if result is None:
        return None
    # No-op: the spliced content equals the base.  Return `base` so
    # the caller records the patch as `already-present` (not
    # `failed`).  See module docstring, ALREADY-APPLIED NO-OP.
    if result == base:
        return base
    if not _valid_python(result):
        return None
    return result


def _try_ts(base, body, lang, path):
    parser = _get_parser(lang)
    if parser is None:
        return None
    try:
        body_tree = parser.parse(body.encode("utf-8"))
    except Exception:
        return None
    if body_tree.root_node.has_error:
        return None
    stmts = list(_iter_ts_stmts(body_tree))
    if len(stmts) != 1:
        return None
    _, _, body_node = stmts[0]
    body_sig = extract_sig(body_node, path)
    if body_sig is None:
        return None

    try:
        base_tree = parser.parse(base.encode("utf-8"))
    except Exception:
        return None
    if base_tree.root_node.has_error:
        return None

    candidates = []
    for start, end, node in _iter_ts_stmts(base_tree):
        s = sig_score(body_sig, extract_sig(node, path))
        if s > 0:
            candidates.append((s, start, end))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    if candidates[0][0] < _MIN_SCORE:
        return None
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < _MIN_GAP:
        return None

    _, start, end = candidates[0]
    base_lines = base.splitlines()
    first_line = base_lines[start]
    indent = first_line[:len(first_line) - len(first_line.lstrip())]
    body_lines = _reindent_to(body.splitlines(), indent)
    result = _splice(base_lines, start, end, body_lines)
    if result is None:
        return None
    # No-op: same already-present semantics as the Python path.
    if result == base:
        return base
    if not _valid_ts(result, lang):
        return None
    return result
