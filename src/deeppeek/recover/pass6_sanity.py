"""pass6_sanity — structural and sanity gates used during pass 6.

STABLE.  These gates protect against the failure mode where a patch
matches correctly but produces semantically broken content:

  · **structural damage** — unbalanced braces inside embedded JS,
    duplicate class nesting inside embedded HTML, module-level
    statements injected at column 0 by a patch that should have
    landed inside a function.

  · **shape damage** — the sanity gate that catches over-deletion
    (result loses >50% of base), empty results, and results with no
    long-line overlap with the base.

The gates are called from `pass6_apply_patches` at the point where
a candidate result exists but has not yet been committed.  A
rejection means the patch is discarded, the record continues with
the previous content, and (for structural damage) the patch status
is set to `skipped_structural_damage` with a specific
`failure_reason`.

Extracted from the pre-2026-11-05 `apply_patches.py` (which combined
the main loop with six distinct gate families).  The main loop lives
in `apply_patches.py`; the insertion/blockquote/dup gates live in
`pass6_gates.py`.

PARSE-CACHE ROUTING (2026-11-05)
================================
`_body_is_wellformed` and `_check_structural_sanity` now route
their `ast.parse` calls through `_ast.parse_source` (memoized).

`_check_structural_sanity` fires once per applied patch — the
largest volume of post-apply parses in the pipeline — and the
`new_content` it parses is exactly the content that `pass8_verify`
will parse again immediately afterwards.  With the cache, the
second parse is a dict lookup.

`_body_is_wellformed`'s parse is on the *patch body* (unique per
patch), so it rarely hits the cache across patches — but it hits
on every reconciliation replay (`_count_failures` iterates
candidate exclusions, each of which replays the patch chain and
calls `_body_is_wellformed` on the same bodies).  On a record with
P patches and an exclusion search over O(P²) trials, the cache
collapses the patch-body parse count from O(P³) to O(P).
"""

import textwrap
from pathlib import Path

from ._ast import parse_source
from .core import _is_deletion_instruction, _looks_like_diff_body


def _body_is_wellformed(body: str, path: str) -> bool:
    """True iff `body` is well-formed for `path`'s destination language.

    Python bodies route through `parse_source` (memoized) so that
    repeated calls — e.g. during reconciliation replay — do not
    re-parse.  JS/HTML bodies fall back to `_js_balance`, which is
    regex-free balance counting.
    """
    if not body or not body.strip():
        return True
    if _looks_like_diff_body(body):
        return True
    if _is_deletion_instruction(body, ""):
        return True
    ext = Path(path).suffix.lower() if path else ""
    if ext == ".py":
        try:
            parse_source(textwrap.dedent(body))
            return True
        except SyntaxError:
            pass
    from .validate_output import _js_balance
    b, p, k, u = _js_balance(body)
    return (b, p, k, u) == (0, 0, 0, 0)


def _check_structural_sanity(old_content, new_content, path,
                              patch_body=""):
    """Refuse a patch that introduces unfixable structural damage.

    Routes the `.py` `ast.parse(new_content)` through `parse_source`
    (memoized).  See the module docstring, PARSE-CACHE ROUTING.
    """
    if not old_content:
        return None
    ext = Path(path).suffix.lower()

    if ext == ".py":
        try:
            parse_source(new_content, filename=path or "<new_content>")
        except SyntaxError as e:
            return (f"patch introduced Python syntax error at "
                    f"line {e.lineno}: {e.msg}")
        from .validate_output import _module_level_stmts_added
        added = _module_level_stmts_added(new_content, old_content)
        if added:
            return (f"patch injected module-level statement(s) at "
                    f"column 0: {added}")
        from .validate_output import (
            _extract_embedded_html, _has_duplicate_class_nesting,
        )
        base_html = _extract_embedded_html(old_content)
        result_html = _extract_embedded_html(new_content)
        if base_html and result_html:
            if (not _has_duplicate_class_nesting(base_html)
                    and _has_duplicate_class_nesting(result_html)):
                return (
                    "patch introduced a duplicate element nested "
                    "inside itself (same `class=` value): the body "
                    "re-opened a wrapper the splice was supposed to "
                    "replace"
                )
        from .patch_bounds import structural_valid
        if not structural_valid(new_content, path):
            return ("patch produced structurally-invalid embedded "
                    "content (JS/HTML parse error inside a Python "
                    "string literal)")
        return None

    if ext in (".js", ".mjs", ".jsx", ".ts", ".tsx", ".html"):
        from .patch_bounds import structural_valid
        if not structural_valid(new_content, path):
            if _body_is_wellformed(patch_body, path):
                return f"patch produced structurally-invalid {ext} content"
        return None
    return None


def _enrich_structural_damage(reason, base, attempted, path, patch_body):
    """Enrich a structural-damage `_check_structural_sanity` reason
    with the AST-level divergence `structural_diff` observed.

    The parser-level reason names *that* the result is malformed.
    The structural diagnostic names *what* changed beyond what the
    patch declared — removed declarations, added elements, duplicate
    nesting.  Both together tell the operator whether the tool
    silently dropped a sibling function or introduced a wrapper that
    shouldn't be there.

    Three cases, distinguished by what `structural_diff` returns:

      · Empty (`""`) — no AST-level divergence.  The failure is a
        pure structural-parse failure (unbalanced braces inside a
        function body, etc.).  The parser-level reason is used
        as-is.

      · A diagnostic-failure string (starts with "structural
        diagnostic raised") — the diagnostic itself crashed.  Rather
        than muddy the reason with an internal-error message, the
        parser-level reason is used as-is and the crash is invisible
        to this path.  (The startup AST check catches the class of
        bug that would cause this.)

      · A divergence string — appended to the reason with an em-dash
        separator: `<parser_reason> — <divergence>`.

    Never raises.  A diagnostic bug must not turn a working pass into
    a crash.
    """
    if not reason:
        return reason
    try:
        from .patch_diag import _diagnose_structural_divergence
        div = _diagnose_structural_divergence(
            base, attempted, path, patch_body)
    except Exception:
        return reason
    if not div or div.startswith("structural diagnostic raised"):
        return reason
    return f"{reason} \u2014 {div}"


def _sanity_gate(base, result):
    from .sanity import is_suspicious
    return is_suspicious(base, result)
