"""
patch_dispatch — the deterministic-patch dispatcher.

`_try_deterministic_patch` is the single entry point the pass-6 main
loop calls for each patch.  It dispatches, in order:

   0. signature-only replacement (before the completeness gate —
      a bare `def foo():` fails `ast.parse` and would otherwise be
      rejected before any heuristic can run)
   1. placeholder-marker gate (refuse or merge — see below)
   2. Python-completeness gate
   3. deletions
   4. raw diffs
   5. import replaces
   6. single-line replaces
   7. scoped replaces
   8. ellipsis splices, with fallback
   9. object-literal splices
  10. named-dict inserts (multi-line entry into a variable-named dict)
  11. context inserts
  12. object-entry inserts (function-anchored)
  13. insert-into-named-scope        (FIX 8 + anchor-derived scope)
  14. insert-before-anchor           (FIX 3)
  15. insert-between-anchors         (FIX 3)
  16. insert-after-anchor            (with `prior_insertions` fallback)
  17. AST anchor (optional dependency)
  18. structural append

PLACEHOLDER-MARKER GATE
=======================
An AI block often contains parenthetical placeholders —
`... (docstring unchanged) ...`, `// ... (rest unchanged)` — that
are instructions to the patcher, not literal code.  Applying them
verbatim replaces the target's content with the marker string.

The gate runs *after* the signature-only check (a signature block
carries no body, so no placeholder) and *before* any other
heuristic.  It attempts to merge:

  · If the body declares a scope (via `_extract_scope_from_body`)
    AND that scope exists in `base`, call
    `patch_scope._try_merge_placeholder_in_scope` to replace the
    marker with the base scope's corresponding content (currently:
    docstring-only merge).  If the merge consumes the placeholder,
    the merged body flows through the rest of the dispatcher as if
    the AI had printed the complete block.

  · Otherwise refuse the block.  A placeholder with no scope to
    merge into (or a placeholder outside a docstring, which the
    current merge does not handle) is not safely applicable.

Prior-insertions threading
==========================
`_try_deterministic_patch` and `_try_deterministic_patch_inner` both
accept a `prior_insertions=None` keyword that is a set of stripped
lines previously inserted by earlier patches in the same pass-6
batch.  It is threaded to `_try_insert_after_anchor`, which uses it
as a fallback anchor source when the prose has no backticked anchor.
Additionally, the pass-6 loop feeds single-line already-present
bodies into `prior_insertions` (see `_mark_already_present` in
`apply_patches.py`), so a bare `output_name = params.get(...)`
that's already in base becomes an anchor for the following
`"Add just after:"` block.

SIGNATURE-ONLY REPLACEMENT
==========================
Runs *first*, before the placeholder gate and completeness gate.
A signature-only block (`def foo(x) -> int:` with no body) fails
`ast.parse` and would be rejected before any heuristic had a
chance.  `_try_signature_replace` handles it directly.
"""

import re

from .core import _is_deletion_instruction, _looks_like_diff_body
from .patch_text import (
    _has_ellipsis_marker, _strip_ellipsis_lines,
    _has_placeholder_marker,
    _would_introduce_empty_block,
    _content_present_in_base,
    _reindent_body,
)
from .patch_scope import (
    _scope_in_base, _apply_scoped, _try_ellipsis_splice,
    _try_merge_placeholder_in_scope,
)
from .patch_heuristics import (
    _try_single_line_replace, _try_context_insert,
    _try_object_entry_insert, _try_insert_after_anchor,
    _try_insert_before_anchor, _try_insert_between_anchors,
    _try_object_literal_splice, _try_raw_diff_patch,
    _try_structural_append, _delete_exact_block,
    _OBJECT_LITERAL_ASSIGNMENT_RE,
    _try_insert_into_named_scope,
    _try_signature_replace,
    _try_named_dict_insert,
)
from .prose import _extract_scope_from_body


# ===========================================================================
#  Import replace
# ===========================================================================

_IMPORT_JOIN_RE = re.compile(
    r'import\s+\{([^}]*)\}\s*from\s*["\']([^"\']+)["\']\s*;?',
    re.DOTALL,
)

_CONTINUATION_ENDINGS = (",", "{", "(", "\\")


def _join_continuations(body: str) -> str:
    """Join lines that are syntactic continuations of a single
    logical line.
    """
    if not body:
        return body
    lines = body.split("\n")
    result = [lines[0]] if lines else [""]
    for line in lines[1:]:
        if not line.strip():
            result.append(line)
            continue
        prev = result[-1].rstrip()
        if prev.endswith(_CONTINUATION_ENDINGS) and line.lstrip():
            result[-1] = prev + " " + line.lstrip()
        else:
            result.append(line)
    return "\n".join(result)


def _try_import_replace(base, raw_body):
    """If body is an import statement and base has an import from the
    same module, replace the base's import with the body.
    """
    joined = _join_continuations(raw_body)
    m = _IMPORT_JOIN_RE.search(joined)
    if not m:
        return None
    module = m.group(2)

    for line in base.split("\n"):
        stripped = line.strip()
        lm = _IMPORT_JOIN_RE.match(stripped)
        if not lm:
            continue
        if lm.group(2) != module:
            continue
        indent = line[:len(line) - len(line.lstrip())]
        reindented = _reindent_body(indent, raw_body.strip())
        result = base.replace(line, reindented, 1)
        return result if result != base else None
    return None


# ===========================================================================
#  Python-completeness gate
# ===========================================================================

_PY_COMPLETE_VERBS = frozenset({
    "try", "with", "for", "while", "if", "def", "class",
    "async", "match",
})

_PY_FRAGMENT_VERBS = frozenset({
    "except", "finally", "else", "elif", "case",
})

_JS_LINE_PREFIXES = (
    "if (", "for (", "while (", "switch (",
    "try {", "catch (", "finally {",
    "function ", "const ", "let ", "var ",
    "export ", "async function ", "await ", "import {", "import *",
)


def _looks_like_js_line(line):
    s = line.strip()
    if not s:
        return False
    if s.startswith(_JS_LINE_PREFIXES):
        return True
    if "=>" in s:
        return True
    if s.endswith("{") and s.count("{") > s.count("}"):
        return True
    return False


def _first_word_of(s):
    out = []
    for ch in s.strip():
        if ch.isalpha() or ch == "_":
            out.append(ch)
        else:
            break
    return "".join(out).lower()


def _is_syntactically_complete_python(body, path):
    if not path.endswith(".py"):
        return True
    stripped = body.strip()
    if not stripped:
        return True
    first = next(
        (ln for ln in stripped.splitlines() if ln.strip()), "")
    if not first:
        return True
    if _looks_like_js_line(first):
        return True
    w = _first_word_of(first.strip())
    if w in _PY_FRAGMENT_VERBS:
        return True
    if w not in _PY_COMPLETE_VERBS:
        return True
    import ast as _ast
    import textwrap as _tw
    try:
        _ast.parse(_tw.dedent(body))
    except SyntaxError:
        return False
    return True


# ===========================================================================
#  Dispatcher
# ===========================================================================

def _try_deterministic_patch(base, patch, path="",
                              prior_insertions=None):
    """Crash-isolated entry point.
    """
    try:
        return _try_deterministic_patch_inner(
            base, patch, path,
            prior_insertions=prior_insertions,
        )
    except Exception as e:
        import sys
        print(f"  [patch] deterministic attempt raised: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def _try_deterministic_patch_inner(base, patch, path="",
                                    prior_insertions=None):
    raw_body = patch["body"]
    body_stripped = raw_body.strip()
    if not body_stripped:
        return None

    # Signature-only replacement.  Runs BEFORE the placeholder and
    # completeness gates: a bare `def foo(x):` is not a complete
    # Python statement, and it contains no placeholder.  This only
    # cares about matching a signature line in base.
    signature_replace = _try_signature_replace(base, raw_body)
    if signature_replace is not None:
        return signature_replace

    # Placeholder gate.  See module docstring.
    if _has_placeholder_marker(raw_body):
        scope_name, _kind = _extract_scope_from_body(body_stripped)
        if scope_name:
            merged = _try_merge_placeholder_in_scope(
                base, scope_name, raw_body)
            if merged is not None and not _has_placeholder_marker(merged):
                # Merge succeeded; treat the merged body as if the AI
                # had written the complete block.
                raw_body = merged
                body_stripped = raw_body.strip()
            else:
                patch["failure_reason"] = (
                    "block contains an AI placeholder marker "
                    "(e.g. `... (docstring unchanged) ...`) inside "
                    f"scope {scope_name!r}, but the base scope's "
                    "content could not be merged in (no matching "
                    "scope in base, or the placeholder is not in a "
                    "docstring position).  Regenerate the block "
                    "with the actual content, or apply manually."
                )
                return None
        else:
            patch["failure_reason"] = (
                "block contains an AI placeholder marker "
                "(e.g. `... (docstring unchanged) ...`) but does "
                "not declare a scope to merge it into.  Regenerate "
                "the block with the actual content, or apply "
                "manually."
            )
            return None

    # Python-completeness gate.
    if not _is_syntactically_complete_python(raw_body, path):
        return None

    context_before = patch.get("context_before", "")

    if _is_deletion_instruction(raw_body, context_before):
        deleted = _delete_exact_block(base, raw_body)
        if deleted is not None:
            return deleted

    if _looks_like_diff_body(raw_body):
        diffed = _try_raw_diff_patch(base, raw_body)
        if diffed is not None:
            return diffed
        return None

    imported = _try_import_replace(base, raw_body)
    if imported is not None:
        return imported

    single_line = _try_single_line_replace(base, raw_body, context_before)
    if single_line is not None:
        return single_line

    _body_is_obj_lit = bool(
        _OBJECT_LITERAL_ASSIGNMENT_RE.match(body_stripped))
    if _body_is_obj_lit:
        _scope_name, _scope_kind = None, None
    else:
        _scope_name, _scope_kind = _extract_scope_from_body(body_stripped)
    if _scope_name and _scope_in_base(base, _scope_name, _scope_kind):
        _scope_result = _apply_scoped(base, _scope_name, body_stripped)
        if _scope_result is not None:
            return _scope_result
        patch["failure_reason"] = (
            f"body declares scope {_scope_name!r} which already "
            f"exists in base, but scoped-replace could not complete"
        )
        return None

    if _has_ellipsis_marker(raw_body):
        result = _try_ellipsis_splice(base, raw_body, context_before)
        if result is not None:
            return result
        stripped_body = _strip_ellipsis_lines(raw_body)
        if not stripped_body.strip():
            return None
        if _would_introduce_empty_block(raw_body, stripped_body):
            return None
        fallback_patch = {**patch, "body": stripped_body}
        result = _try_deterministic_patch_inner(
            base, fallback_patch, path,
            prior_insertions=prior_insertions,
        )
        if result is not None:
            return result

        first_line = next(
            (ln.strip() for ln in stripped_body.splitlines() if ln.strip()),
            "")
        if (first_line and first_line not in base
                and _content_present_in_base(base, stripped_body,
                                              threshold=0.5)):
            patch["stale_drift"] = True
            return base
        return None

    m = _OBJECT_LITERAL_ASSIGNMENT_RE.match(body_stripped)
    if m:
        id_m = re.search(
            r'([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*=\s*$',
            m.group("prefix").rstrip(),
        )
        if id_m:
            return _try_object_literal_splice(
                base, body_stripped, id_m.group(1))
        return None

    named_dict = _try_named_dict_insert(base, raw_body, context_before)
    if named_dict is not None:
        return named_dict

    inserted = _try_context_insert(base, raw_body)
    if inserted is not None:
        return inserted

    inserted = _try_object_entry_insert(base, raw_body, context_before)
    if inserted is not None:
        return inserted

    inserted = _try_insert_into_named_scope(
        base, raw_body, context_before, path)
    if inserted is not None:
        return inserted

    inserted = _try_insert_before_anchor(
        base, raw_body, context_before, path=path)
    if inserted is not None:
        return inserted

    inserted = _try_insert_between_anchors(
        base, raw_body, context_before, path=path)
    if inserted is not None:
        return inserted

    inserted = _try_insert_after_anchor(
        base, raw_body, context_before,
        prior_insertions=prior_insertions,
        path=path,
    )
    if inserted is not None:
        return inserted

    if path:
        try:
            from .ast_anchor import try_ast_anchor
        except ImportError:
            try_ast_anchor = None
        if try_ast_anchor is not None:
            ast_result = try_ast_anchor(base, raw_body, path, context_before)
            if ast_result is not None:
                line_idx, indent, evidence = ast_result
                patch["ast_evidence"] = evidence
                reindented = _reindent_body(
                    indent, raw_body.lstrip("\n").rstrip())
                lines = base.split("\n")
                new_lines = reindented.split("\n")
                result = "\n".join(
                    lines[:line_idx] + new_lines + lines[line_idx:])
                if result != base:
                    return result

    if path:
        structural = _try_structural_append(base, raw_body, path)
        if structural is not None:
            import sys
            print(f"  [structural] {path}: appended by structure",
                  file=sys.stderr)
            patch["structural_evidence"] = (
                "text-structural append (step 9)")
            return structural

    return None


# ===========================================================================
#  Sanity failure attribution
# ===========================================================================

def attribute_sanity_failure(rec, blocks_by_idx, validate_fn=None):
    """Return the idx of the first patch whose application causes
    validation to fail, or None if attribution isn't possible.
    """
    if validate_fn is None:
        from .validate_output import validate_content as validate_fn

    if not rec.patches:
        return None
    base_block = (blocks_by_idx.get(rec.last_full_idx)
                  if rec.last_full_idx is not None else None)
    if base_block is None:
        return None
    base_content = base_block.body
    if base_content is None:
        return None

    current = base_content
    for patch_dict in rec.patches:
        applied_by = patch_dict.get("applied_by")
        if applied_by == "already-present":
            continue
        if applied_by == "failed":
            continue
        if applied_by != "deterministic":
            return None
        result = _try_deterministic_patch(current, patch_dict, rec.path)
        if result is None:
            continue
        if result == current:
            continue
        current = result
        if validate_fn(current, rec.path) is not None:
            return patch_dict["idx"]
    return None
