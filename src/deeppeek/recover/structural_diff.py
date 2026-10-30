# src/deeppeek/recover/structural_diff.py
"""
Compares base and result at the structural level, not the line level.

Two roles:

  1. Post-apply gate (pass 8.10).  For every record with patches
     applied, walk the base and result ASTs/HTML trees and diff their
     top-level "skeleton".  Any declaration or element that was *added*
     that wasn't introduced by the patch body — or any element that
     was *replaced by a nested duplicate of itself* — is a fail-closed
     signal for reconciliation.

  2. Attribution diagnostic.  When a patch fails, this pass names
     the exact structural divergence ("your patch added a second
     `<div class='field'>` inside an existing one") instead of
     producing a line-based "no matching anchor".

Two languages supported today — extend with a new parser, not a new
pass:

    .py    ast.parse + tree_sitter_python (whichever is available)
    .html  tree_sitter_html
    .py+embedded html
    .js    tree_sitter_javascript

The pass is a single file: adding a language means adding a
`_signature_<ext>` helper.  Nothing else in the pipeline needs to know.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SignatureChange:
    kind:      str   # "added" | "removed" | "duplicated-nesting"
    lang:      str   # "python" | "html" | "js"
    signature: str   # e.g. "function foo", "div.field", "div.field[align-items:center;]"
    detail:    str   # human-readable hint


def _python_statement_sigs(content):
    """Return {"python_stmt": {sig_repr, ...}} for every top-level
    statement inside function bodies, keyed by a one-line repr of the
    statement's signature dict.  The `repr` is what the divergance
    list reports; the point is that `return {"success": False, ...}`
    and `return {"success": True, ...}` produce *different* entries
    and the pass can therefore see one was removed and the other
    added.
    """
    from .statement_sig import _python_sig
    import ast, textwrap
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    out = set()
    def _walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Return, ast.Assign, ast.AnnAssign,
                                  ast.Expr)):
                sig = _python_sig(child)
                if sig is not None:
                    out.add(repr(sig))
            if hasattr(child, "body"):
                _walk(child)
    _walk(tree)
    return out


def extract_signatures(content: str, path: str) -> dict[str, set[str]]:
    """Return `{lang: {signature, ...}}` for the content.

    Signatures are canonical one-liners:
        python: "def foo" / "class Bar" / "async def baz"
        html:   "div.field" / "span[align-items:center;]"
        js:     "function foo" / "class Bar" / "const baz"
    """
    out: dict[str, set[str]] = {}
    ext = Path(path).suffix.lower()

    if ext == ".py":
        out["python"] = _python_signatures(content)
        out["python_stmt"] = _python_statement_sigs(content)
        out["html"] = _embedded_html_signatures(content)
    elif ext == ".html":
        out["html"]   = _html_signatures(content)
    elif ext in (".js", ".mjs", ".ts", ".tsx", ".jsx"):
        out["js"]     = _js_signatures(content)
        out["html"]   = _embedded_html_signatures(content)
    return out


def structural_diff(base: str, result: str, path: str,
                    patch_body: str) -> list[SignatureChange]:
    """Return structural changes the *patch* is responsible for.

    Skips changes that also appear inside the patch's own body —
    those are the intended additions.  Everything else is a
    divergence the patch introduced beyond what it declared.
    """
    base_sigs   = extract_signatures(base,   path)
    result_sigs = extract_signatures(result, path)
    body_sigs   = extract_signatures(patch_body, path)
    changes: list[SignatureChange] = []

    for lang, base_set in base_sigs.items():
        result_set = result_sigs.get(lang, set())
        body_set   = body_sigs.get(lang, set())

        # Removed signatures: present in base, missing from result,
        # and not declared as removed by the patch body itself.
        for sig in base_set - result_set:
            if sig in body_set:
                # The patch removed it deliberately (body's presence
                # signals intent — a body that names a signature in
                # the same position it removes it is a replace).
                continue
            changes.append(SignatureChange(
                kind="removed", lang=lang, signature=sig,
                detail=f"{sig!r} was in the base but is missing from "
                       f"the result — the patch removed it without "
                       f"declaring it.",
            ))

        # Added signatures that aren't declared by the patch body:
        # the tool added something the conversation didn't ask for.
        for sig in result_set - base_set:
            if sig in body_set:
                continue
            changes.append(SignatureChange(
                kind="added", lang=lang, signature=sig,
                detail=f"{sig!r} is present in the result but not the "
                       f"base and not declared by the patch body — "
                       f"the tool introduced it.",
            ))

    # HTML-specific: duplicate nesting check.
    if path.endswith((".py", ".html")):
        base_html   = _extract_embedded_html(base) or base
        result_html = _extract_embedded_html(result) or result
        if (_has_duplicate_class_nesting(result_html)
                and not _has_duplicate_class_nesting(base_html)):
            changes.append(SignatureChange(
                kind="duplicated-nesting", lang="html",
                signature="(nested class)",
                detail="the patch nested an element inside a duplicate "
                       "of itself — a wrapper that should have been "
                       "replaced was retained alongside the new copy.",
            ))

    return changes
