"""
patch_dispatch — the deterministic-patch dispatcher.

`_try_deterministic_patch` is the single entry point the pass-6 main
loop calls for each patch.  It dispatches, in order:

  1. deletions (if the body / prose is a deletion instruction)
  2. raw diffs
  3. import replaces (multi-line import -> single-line import and
     vice versa)
  4. single-line replaces
  5. scoped replaces (if the body declares a scope that exists in base)
  6. ellipsis splices, with fallback
  7. object-literal splices
  8. context inserts
  9. object-entry inserts
 10. insert-after-anchor
 11. AST anchor (optional dependency)
 12. structural append

IMPORT REPLACE (new)
====================
A multi-line import body whose module matches a single-line import
in base would previously fall through every heuristic:

  · `_try_single_line_replace` requires exactly one non-blank line.
  · `_try_context_insert` needs a unique first or last line — but
    both lines in a multi-line import are continuations, not lines
    that appear in base.

The pg_panel_bindings.py failure case: body is

    import { board, setImage, clearImage, _scheduleDraw,
             view, _applyRotation } from "./pg_state.js";

and base has

    import { board, setImage, clearImage, _scheduleDraw } from "./pg_state.js";

`_try_import_replace` joins continuation lines, parses the import,
finds the same-module import in base, and reindents the original
body to match.  It intentionally ignores the "add"/"insert" prose
blocklist that `_try_single_line_replace` respects — adding a name
to an import list IS a replacement of the import statement.

Also exports `attribute_sanity_failure`, the helper that names the
patch responsible for a sanity-gate rejection.  It lives here
because it needs both `_try_deterministic_patch` (for the replay)
and the block/record shapes.

Imports from patch_text, patch_scope, patch_heuristics, patch_diag,
and ast_anchor (lazy, optional).
"""

import re

from .core import _is_deletion_instruction, _looks_like_diff_body
from .patch_text import (
    _has_ellipsis_marker, _strip_ellipsis_lines,
    _would_introduce_empty_block,
    _content_present_in_base,
    _reindent_body,
)
from .patch_scope import (
    _scope_in_base, _apply_scoped, _try_ellipsis_splice,
)
from .patch_heuristics import (
    _try_single_line_replace, _try_context_insert,
    _try_object_entry_insert, _try_insert_after_anchor,
    _try_object_literal_splice, _try_raw_diff_patch,
    _try_structural_append, _delete_exact_block,
    _OBJECT_LITERAL_ASSIGNMENT_RE,
)
from .prose import _extract_scope_from_body


# ===========================================================================
#  Import replace
# ===========================================================================

# A single import statement.  `[^}]*` for the names inside `{}` (no
# nested braces in an import list — an import alias is `A as B`, no
# expression syntax).  `[^"']+` for the module path (no quotes inside
# a module path).  `;?` because some projects omit the semicolon.
_IMPORT_JOIN_RE = re.compile(
    r'import\s+\{([^}]*)\}\s*from\s*["\']([^"\']+)["\']\s*;?',
    re.DOTALL,
)

# Continuations of a single logical line: end with `,`, `{`, `(`,
# or backslash.  Used by `_join_continuations` so a 2-line import is
# parsed as 1 logical line.
_CONTINUATION_ENDINGS = (",", "{", "(", "\\")


def _join_continuations(body: str) -> str:
    """Join lines that are syntactic continuations of a single
    logical line.

    A continuation line ends with `,`, `{`, `(`, or `\\`.  The next
    line is appended to the previous line's right side.  Blank lines
    start a fresh logical line.
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

    Intentionally ignores the `add`/`insert` prose blocklist that
    `_try_single_line_replace` respects: adding a name to an import
    list IS a replacement of the import statement, regardless of how
    the prose frames it.

    Returns the patched base, or None.

    The body may be single-line or multi-line — a continuation line
    (ending with `,`/`{`/`(`/`\\`) is joined to its predecessor for
    the purposes of parsing.  The ORIGINAL multi-line body is what
    gets inserted, reindented to the base's import indent.
    """
    joined = _join_continuations(raw_body)
    m = _IMPORT_JOIN_RE.search(joined)
    if not m:
        return None
    module = m.group(2)

    # Locate the matching import in base.  We iterate lines (single-
    # line import per line) rather than re-running the regex over the
    # whole base because the base may contain *other* import
    # statements; we want the one from the same module.
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
#  Dispatcher
# ===========================================================================

def _try_deterministic_patch(base, patch, path=""):
    """Crash-isolated entry point.

    A crash inside any of the inner heuristics is caught here and
    turned into a `None` result, which the caller records as a failed
    patch.  Without this wrapper, one bad block shape can take
    down the entire pass for every file in the conversation.
    """
    try:
        return _try_deterministic_patch_inner(base, patch, path)
    except Exception as e:
        import sys
        print(f"  [patch] deterministic attempt raised: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def _try_deterministic_patch_inner(base, patch, path=""):
    raw_body = patch["body"]
    body_stripped = raw_body.strip()
    if not body_stripped:
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
        # A diff body is a diff, not a scope replacement.  If
        # `_try_raw_diff_patch` failed, fall through to the
        # already-present / failed classification.
        return None

    # NEW: import replace.  Runs before the generic single-line path
    # because a multi-line import is not a single line and would
    # otherwise be blocked by every context-insert check.  See
    # module docstring.
    imported = _try_import_replace(base, raw_body)
    if imported is not None:
        return imported

    # Single-line replace
    single_line = _try_single_line_replace(base, raw_body, context_before)
    if single_line is not None:
        return single_line

    # Scope-aware pre-check
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

    # Ellipsis handling
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
        result = _try_deterministic_patch_inner(base, fallback_patch, path)
        if result is not None:
            return result

        # DRIFT-FIX: stale-ellipsis detection.
        #
        # Enter this branch only when:
        #   · the block has an ellipsis marker (already true here);
        #   · the fallback insert also failed (just happened);
        #   · the block's first content line is gone from base
        #     (checked below); AND
        #   · most of the block's other content is still in base.
        #
        # Together these describe "the base already contains what this
        # block was trying to change, in a slightly different form."
        # The typical cause is a stale ellipsis block from an earlier
        # turn whose "before" content was superseded by a pair that
        # ran first in this batch.
        #
        # Returning base (unchanged) lets the caller's `result ==
        # content` check mark the patch `already-present`.  Fail-
        # closed semantics are preserved: a genuinely new block that
        # just has no anchor fails the second condition and returns
        # None as today.
        first_line = next(
            (ln.strip() for ln in stripped_body.splitlines() if ln.strip()),
            "")
        if (first_line and first_line not in base
                and _content_present_in_base(base, stripped_body,
                                              threshold=0.5)):
            patch["stale_drift"] = True
            return base
        return None

    # Object-literal splice
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

    inserted = _try_context_insert(base, raw_body)
    if inserted is not None:
        return inserted

    inserted = _try_object_entry_insert(base, raw_body, context_before)
    if inserted is not None:
        return inserted

    inserted = _try_insert_after_anchor(base, raw_body, context_before)
    if inserted is not None:
        return inserted

    # AST anchor
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

    # Structural append
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

    Returns None when:
      - rec.patches is empty;
      - rec.last_full_idx is unset or not in blocks_by_idx;
      - a patch was applied by LLM (no faithful replay);
      - a patch cannot be replayed deterministically;
      - the base itself already fails validation (nothing to blame).
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
        if applied_by != "deterministic":
            return None
        result = _try_deterministic_patch(current, patch_dict, rec.path)
        if result is None:
            return None
        if result == current:
            continue
        current = result
        if validate_fn(current, rec.path) is not None:
            return patch_dict["idx"]
    return None
