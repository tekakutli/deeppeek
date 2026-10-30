"""
statement_sig — structural identity of one statement.

A "statement signature" is a hashable description of *what a statement
is*, independent of its surrounding context, whitespace, or
indentation.  Two statements with matching signatures are candidates
for replace-in-place: one is a modified version of the other.

Used by:

  · `patch_statement_replace.try_statement_replace` — places a
    single-statement body over a base statement that carries the same
    signature.
  · `structural_diff` — extends the pass-8.10 audit from
    declaration-level to statement-level: "the patch replaced a
    Return whose signature was X with one whose signature is Y" is a
    divergence a human can act on; "both statements parse" is not.

The signature itself is deliberately small: node type + identity
tokens (dict keys with literal values, assignment targets, callable
name + keyword names).  Structural overlap on the identity tokens is
what separates a real replace from a coincidental line match.

SCORING (2026-10-29 revision)
=============================
Dict-bearing statements use `0.4·key_overlap + 0.6·value_overlap`
with NO kind-base reward.  The 0.30 base reward present in the
pre-fix version flattens the score gap between a target and its
token-coincidental neighbours below `_MIN_GAP`, causing the
heuristic to fail closed on exactly the case it exists for.  See
`patch_dispatch.py`'s STRUCTURAL STATEMENT REPLACE PRE-CHECK
section for the motivating numbers.

Non-dict statements (call expressions, bare returns, target names)
keep the 0.30·kind base: their signatures are sparser (no literal
values to differentiate), so the kind reward is load-bearing for
the margin to be meaningful at all.

LANGUAGE SUPPORT
================
Python: `ast` module (always available).
JS/TS: tree-sitter via `ast_anchor._get_parser`.  When the grammar is
unavailable, `extract()` returns None and every caller falls through —
same graceful-degradation contract as `ast_anchor.try_ast_anchor`.

Adding a language is one branch in `_python_sig` / `_tree_sitter_sig`;
the scoring and matching are language-agnostic.
"""
from __future__ import annotations
import ast
from pathlib import Path
from typing import Optional

from .ast_anchor import _get_parser, _LANG_BY_EXT


def _value_sig(node) -> tuple:
    """Hashable descriptor for a Python value node."""
    if isinstance(node, ast.Constant):
        return ("const", repr(node.value))
    if isinstance(node, ast.Name):
        return ("name", node.id)
    if isinstance(node, ast.Attribute):
        return ("attr", node.attr, _value_sig(node.value))
    if isinstance(node, ast.Subscript):
        return ("sub", _value_sig(node.value))
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return ("call", node.func.id)
        if isinstance(node.func, ast.Attribute):
            return ("meth", node.func.attr)
        return ("call", None)
    if isinstance(node, ast.BinOp):
        return ("bin", type(node.op).__name__)
    return ("other", type(node).__name__)


def _dict_sig(node) -> dict:
    """{key: value_sig} for a Python Dict node."""
    if not isinstance(node, ast.Dict):
        return {}
    out = {}
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out[k.value] = _value_sig(v)
    return out


def _python_sig(node) -> Optional[dict]:
    """Signature for one Python statement node."""
    if isinstance(node, ast.Return):
        d = _dict_sig(node.value) if node.value else {}
        if d:
            return {"kind": "return", "dict": d}
        return {"kind": "return", "expr": type(node.value).__name__
                if node.value else None}

    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target])
        names = []
        for t in targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Attribute):
                names.append(t.attr)
        d = _dict_sig(node.value) if node.value else {}
        if names or d:
            return {"kind": "assign", "targets": tuple(sorted(names)),
                    "dict": d}

    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        c = node.value
        name = None
        if isinstance(c.func, ast.Name):
            name = c.func.id
        elif isinstance(c.func, ast.Attribute):
            name = c.func.attr
        return {
            "kind": "call",
            "name": name,
            "kwargs": tuple(sorted(kw.arg for kw in c.keywords if kw.arg)),
            "n_pos": len(c.args),
        }

    return None


_JS_STATEMENT_TYPES = {
    "return_statement", "expression_statement", "lexical_declaration",
    "variable_declaration", "if_statement", "for_statement",
    "while_statement", "try_statement",
}


def _text(node) -> str:
    t = node.text
    if isinstance(t, bytes):
        return t.decode("utf-8", "replace")
    return t or ""


def _tree_sitter_sig(node, lang: str) -> Optional[dict]:
    """Signature for one tree-sitter node (JS/TS).  Minimal but general:
    match on statement type + declared/assigned identifiers.  HTML uses
    a different signature family — tag + class + id + child-structure
    count — and is left for a follow-up.
    """
    if node.type not in _JS_STATEMENT_TYPES:
        return None
    if node.type == "return_statement":
        # Return expression text, whitespace-normalized, is the
        # signature for JS returns (no dict-key identity without
        # deeper parsing).
        return {"kind": "return", "text": " ".join(_text(node).split())}
    if node.type in ("lexical_declaration", "variable_declaration"):
        names = []
        for child in node.children:
            if child.type == "variable_declarator":
                for c in child.children:
                    if c.type == "identifier":
                        names.append(_text(c))
        return {"kind": "decl", "targets": tuple(sorted(names))}
    if node.type == "expression_statement":
        for c in node.children:
            if c.type == "call_expression":
                fn = ""
                for cc in c.children:
                    if cc.type == "identifier":
                        fn = _text(cc)
                        break
                return {"kind": "call", "name": fn}
    return {"kind": node.type}


def extract(node, path: str):
    """Signature for one statement node in any supported language.

    `node` is an `ast.AST` for `.py` destinations, or a tree-sitter
    `Node` for `.js` / `.mjs` / `.ts` / `.tsx` / `.html`.  Returns
    None when the node has no useful signature or the language is
    unknown — callers treat None as "not a candidate".
    """
    ext = Path(path).suffix.lower() if path else ""
    if ext == ".py":
        if not isinstance(node, ast.AST):
            return None
        return _python_sig(node)
    lang = _LANG_BY_EXT.get(ext)
    if lang and hasattr(node, "type"):
        return _tree_sitter_sig(node, lang)
    return None


def score(a: Optional[dict], b: Optional[dict]) -> float:
    """Structural similarity in [0, 1].

    Scoring rubric (dict-bearing statements):
      · identical kind (gate)           → required, else 0.0
      · shared dict keys                → 0.40 (fraction overlap)
      · shared literal values           → 0.60 (fraction overlap)
      Total = 0.4·key + 0.6·value; no kind base reward.
      Rationale: see patch_dispatch.py STRUCTURAL STATEMENT REPLACE
      PRE-CHECK.

    Scoring rubric (non-dict statements):
      · identical kind                  → 0.30
      · shared targets / kwarg names    → up to 0.40 / 0.20
      · shared callable name            → up to 0.20

    A matching `kind` with no identity overlap scores 0.30 (non-dict)
    or 0.0 (dict-bearing) and is rejected by callers as "no match".
    """
    if a is None or b is None:
        return 0.0
    if a.get("kind") != b.get("kind"):
        return 0.0

    # Dict-bearing branch: no kind base reward.  See docstring.
    a_dict = a.get("dict") or {}
    b_dict = b.get("dict") or {}
    if a_dict or b_dict:
        a_keys = set(a_dict)
        b_keys = set(b_dict)
        if not a_keys or not b_keys:
            return 0.0
        key_overlap = len(a_keys & b_keys) / len(a_keys | b_keys)
        if key_overlap < 0.3:
            return 0.0
        value_overlap = (
            sum(1 for k in a_keys if a_dict.get(k) == b_dict.get(k))
            / len(a_keys)
        )
        return min(0.4 * key_overlap + 0.6 * value_overlap, 1.0)

    # Non-dict branch: kind base reward is load-bearing for the
    # sparser signatures to clear _MIN_GAP meaningfully.
    s = 0.30

    a_tgt = set(a.get("targets") or [])
    b_tgt = set(b.get("targets") or [])
    if a_tgt or b_tgt:
        if a_tgt and b_tgt:
            s += 0.40 * (len(a_tgt & b_tgt) / len(a_tgt | b_tgt))
        if not (a_tgt & b_tgt):
            return 0.0

    a_kw = set(a.get("kwargs") or [])
    b_kw = set(b.get("kwargs") or [])
    # NEW (2026-10-29): an added kwarg (one side empty, the other
    # non-empty) is a real modification — do not reject it as a
    # non-match.  The bonus is naturally zero because the
    # intersection is empty, so the score stays at the kind base
    # plus any target overlap.  Only both-non-empty-and-disjoint
    # is a genuine non-match.
    if a_kw and b_kw and not (a_kw & b_kw):
        return 0.0
    if a_kw or b_kw:
        union = a_kw | b_kw
        if union:
            s += 0.20 * (len(a_kw & b_kw) / len(union))

    if a.get("name") and a["name"] == b.get("name"):
        s += 0.20

    return min(s, 1.0)
