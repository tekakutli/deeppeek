"""
patch_dispatch — the deterministic-patch dispatcher.

`_try_deterministic_patch` is the single entry point the pass-6 main
loop calls for each patch.  It dispatches, in order:

   0. signature-only replacement (before the completeness gate —
      a bare `def foo():` fails `ast.parse` and would otherwise be
      rejected before any heuristic can run)
   0a. already-present pre-check (2026-10-28f — see below)
   0b. multi-statement apply (2026-10-28g — see below)
   1. placeholder-marker gate (refuse or merge — see below)
   2. Python-completeness gate
   3. deletions
   4. raw diffs
   5. import replaces
   6. single-line replaces
   6a. indented def/class replaces (2026-10-28g — see below)
   7. scoped replaces
   8. ellipsis splices, with fallback
   9. object-literal splices (with trailing-content refusal)
  10. named-dict inserts (multi-line entry into a variable-named dict)
  11. context inserts
  12. object-entry inserts (function-anchored)
  13. insert-into-named-scope        (FIX 8 + anchor-derived scope)
  14. insert-before-anchor           (FIX 3)
  15. insert-between-anchors         (FIX 3)
  16. insert-after-anchor            (with `prior_insertions` fallback
                                      + `prior_scopes` disambiguation)
  17. AST anchor (optional dependency)
  18. structural append

MULTI-STATEMENT APPLY (2026-10-28g)
===================================
A body that contains two or more adjacent top-level statements — e.g.
a new assignment followed by a new function declaration — can't be
handled by any single heuristic.  The object-literal splice would
silently drop the function (only the assignment's object literal
would be merged into base); the insert heuristics reject bodies that
carry their own scope declarations; and the completeness gate passes
the whole body as a single unit.

Concrete motivating shape (Fix 3 in the 2026-10-28 comfyui.py
recovery):

    captured = {"image_b64": None, "message": "", "errors": []}

    def on_progress(index, result):
        cmd = result.get("command")
        ...

The pre-fix behaviour: `_OBJECT_LITERAL_ASSIGNMENT_RE` matched the
`captured = {` prefix, `_try_object_literal_splice` consumed the whole
body (silently truncating at the object literal's closer), the
resulting spliced base was missing the new `errors` key AND missing
the new `on_progress` body, and pass6 either applied that corrupt
result or failed closed depending on whether the truncation happened
to be valid Python.

Fix: `_try_multi_statement_apply` splits the body into individual
top-level statements and applies each one through the normal
dispatcher.  A sub-statement that fails to place causes the whole
multi-statement attempt to bail (fail-closed — no partial mutations).

Recursion is prevented by the module-level `_in_multi_statement`
flag: sub-statements do NOT recursively attempt multi-statement
apply.  Assumes single-threaded execution — true of the pipeline's
pass6 loop, but flagged here for future readers.

INDENTED DEF/CLASS REPLACE (2026-10-28g)
========================================
A body that starts with a `def name(...)` / `async def` / `class`
declaration at column 0 is normally routed to scoped-replace
(`_apply_scoped`), which requires the name to appear as a
column-0 declaration in base.  When the name exists only *indented*
inside another function (a nested def), scoped-replace declines and
the insert heuristics would append the new definition inside the
enclosing function — producing TWO copies of the same-named def.

Concrete motivating shape (again Fix 3): the base has
`def on_progress(...)` nested inside `def handle_inpaint(...)`, and
the body is a full column-0 reprint of `def on_progress(...)`.
Without indented-def-replace, AST-anchor picks `handle_inpaint` as
the container and inserts the new `on_progress` at its end —
duplicating the function.

Fix: `_try_indented_def_replace` finds the unique indented match,
computes the line range, and replaces it in place.  Fires only
when the name has no column-0 declaration (scoped-replace owns
the top-level case) and exactly one indented match exists.

INDENTED-DEF REINDENT BUG (2026-11-01)
======================================
The pre-2026-11-01 dispatcher called
`_try_indented_def_replace(base, body_stripped)` — body_stripped
being the raw body with the first line's leading whitespace removed
(and the rest of the body still carrying its fence-level indent).

`_try_indented_def_replace` then calls
`_reindent_body(indent_str, body)`.  Inside `_reindent_body`:

    orig_indent_len = len(first) - len(first.lstrip(" \t"))
    adjusted[0]     = base_indent + first.lstrip(" \t")
    adjusted[i>0]   = base_indent + " " * (ln_indent - orig_indent_len) + ...

When `body` is `body_stripped`, the first line is column 0, so
`orig_indent_len = 0`.  Subsequent lines carry the fence's original
indent (e.g. 8 spaces inside a `def` at 4-space indent), so
`ln_indent - orig_indent_len = 8`, and `adjusted` puts each line at
`base_indent + 8 spaces` — 12 spaces at base_indent = 4, where 8 is
correct.

Passing `raw_body` (which preserves the fence's original indentation
in the first line) makes `orig_indent_len` match the body's natural
indent, so `_reindent_body` correctly offsets the internal lines.

The motivating case (comfyui.py, `comfy_set_layer_pixels`): the
body's first line is `    def comfy_set_layer_pixels(self, params):`
and subsequent lines are at 8-space indent.  With `body_stripped` the
reindented result has the def at 4 spaces and its body at 12 spaces;
Python parses that (12 > 4 = valid indentation), so the bug is
silent — the output looks structurally correct but the code is
2× overindented.  With `raw_body` the reindented def and its body
land at the intended 4 and 8 spaces.

OBJECT-LITERAL TRAILING-CONTENT REFUSAL (2026-10-28g, refined 2026-10-28h)
===========================================================================
`_try_object_literal_splice` consumes a body's `x = {...}` prefix.
When the body has additional *statements* after the object literal,
the splice silently truncates.  A pre-check refuses the body when it
has non-comment content after the object literal's closer, falling
through so `_try_multi_statement_apply` (which runs earlier and
would have already split this shape) — or an honest failure —
handles it.

REFINEMENT (2026-10-28h): a bare trailing `;` is the JS/C-family
statement terminator, not "trailing content".  The initial version
of the refusal treated `export const foo = {...};` as having trailing
content (`;`) and refused it — regressing fixture 02
(`02_subsection_patches`), fixture 04 (`04_prose_override`), and
fixture 35 (`35_object_literal_replace`), all of which use
`export const NAME = {...};` or `NAME = {...};` bodies.

The refined check strips a single leading `;` from the tail before
running the comment check, so:

    x = {...}                        → splice fires
    x = {...};                       → splice fires (trailing `;`)
    x = {...};  # note               → splice fires (comment)
    x = {...};\ndef foo(): ...       → refused (trailing def)
    x = {...}\ndef foo(): ...        → refused (trailing def)

DEFERRED PREDICATES (2026-10-28f hotfix)
========================================
The already-present pre-check is *skipped* for two patch shapes:

  · Deletion instructions (`_is_deletion_instruction(body, ctx)`):
    the body IS the content that should disappear.  It's trivially
    "already present" — because it's the thing being removed.  Letting
    the pre-check fire here treats the deletion as a no-op and the
    block is never deleted.  Regression observed in
    `tests/recover/test_recover.py::test_07` (deletion shapes):
    every deletion patch became `already-present`, the `.panelState`
    declaration stayed in the file, and `_function_presence_check`
    then sanity-rejected the record because the record's body didn't
    remove it (the sanity path's `allow_missing` is populated only
    from `applied_by == "deterministic"` patches, and `already-present`
    patches don't count).

  · Diff bodies (`_looks_like_diff_body(body)`): a diff represents
    "transform base into something else" via `-`/`+`/` ` prefixes.
    The literal diff text (including `-` prefixed lines) is never
    present in base as-is, so the multi-line token check would pass
    by accident on the bare content — but the pre-check's *intent*
    ("skip if applied") is already covered by `_diff_already_applied`
    downstream, which correctly distinguishes "the additions are
    present, the deletions are gone" from "the additions are present
    but the deletions still exist".

Deletions and diffs have their own already-applied predicates
(`_deletion_already_applied`, `_diff_already_applied`); those run at
the appropriate point in the dispatcher and produce the correct
`already-present` outcome without blocking the deletion/diff from
firing in the first place.

ALREADY-PRESENT PRE-CHECK (2026-10-28f)
=======================================
An early check that runs after signature replace and before any
heuristic fires.  If the patch's content is already present in base,
returning base (no change) lets the outer loop mark the patch
`already-present` instead of letting a heuristic re-apply it and
duplicate content.

Two tiers, per `patch_already.py`'s module docstring:
  · Short bodies (< 3 lines): `_exact_lines_present` (sliding-window
    only; token fallback false-positives on single-line import changes)
  · Multi-line bodies (>= 3 lines): `_max_contiguous_run >= 95% of
    body lines` AND `_patch_already_present` (85% token presence).
    The contiguity gate keeps comfyui.py's `idx=97` try/except — which
    introduces `output_rotation` (new tokens) but whose tokens are
    otherwise dispersed in base — from being falsely skipped.

The pre-check can be fooled by prose that names the same identifiers
without the same structure.  When it fires wrongly, the patch is
marked `already-present` and the dispatcher's caller (pass 6)
records it as a no-op.  Pass 8.7d (consistency.py) re-verifies every
such claim against the final file content; false positives are
flipped back to `failed` and the record is demoted.  See that
module's docstring for the motivating comfyui.py case.

PLACEHOLDER-MARKER GATE
=======================
An AI block often contains parenthetical placeholders —
`... (docstring unchanged) ...`, `// ... (rest unchanged)` — that
are instructions to the patcher, not literal code.  Applying them
verbatim replaces the target's content with the marker string.

The gate runs *after* the signature-only check and *before* any other
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
batch, and a `prior_scopes=None` keyword that is a dict mapping each
such line to the scope name extracted from its Find-block prose.

`prior_insertions` is threaded to `_try_insert_after_anchor`, which
uses it as a fallback anchor source when the prose has no backticked
anchor.  `prior_scopes` lets that fallback disambiguate an anchor
that appears multiple times in the base: when `base.count(stripped)`
is greater than 1, the fallback calls
`patch_anchor_insert._anchor_within_scope` / `_anchor_within_callee`
with the recorded scope name (see that module's PRIOR-INSERTIONS
SCOPE CONTEXT section, and the 2026-10-28f correction there for
scope-first ordering).

SIGNATURE-ONLY REPLACEMENT
==========================
Runs *first*, before the already-present pre-check and completeness
gate.  A signature-only block (`def foo(x) -> int:` with no body)
fails `ast.parse` and would be rejected before any heuristic had a
chance.  `_try_signature_replace` handles it directly.  Because a
signature-only body is a *fragment* (its body lives in base), the
already-present pre-check would be a false positive on it — hence
signature replace must run before it.
"""

import re

from .core import _is_deletion_instruction, _looks_like_diff_body
from .patch_text import (
    _has_ellipsis_marker, _strip_ellipsis_lines,
    _has_placeholder_marker,
    _would_introduce_empty_block,
    _content_present_in_base,
    _reindent_body,
    _find_matching_brace,
    _looks_like_python_fragment,
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
from .patch_already import (
    _patch_already_present,
    _exact_lines_present,
    _max_contiguous_run,
)
from .prose import _extract_scope_from_body


# ===========================================================================
#  Multi-statement apply (2026-10-28g)
# ===========================================================================
#
# Module-level flag to prevent infinite recursion.  Assumes single-
# threaded execution — true of the pass6 loop, false of a hypothetical
# concurrent caller.  If threading ever becomes a concern, replace
# with a `threading.local`.
_in_multi_statement = False


def _split_python_statements(body: str) -> list:
    """Split Python source into top-level statements.

    A "statement" starts at column 0 and is a def/class, an assignment,
    a decorated function, or a bare import/expression.  Blank lines
    between statements are separators, not content.  Comment-only
    chunks are merged into the next statement (a leading comment
    belongs with the statement it annotates).

    Refuses bodies containing triple-quoted strings: the regex-based
    splitter can't tell a top-level `def` inside a string literal from
    a real one, so `PYCODE = r\"\"\"...def foo():...\"\"\"` would falsely
    split.  Those bodies fall through to the single-statement
    heuristics (or fail closed).
    """
    if '"""' in body or "'''" in body:
        return []

    lines = body.split("\n")
    stmts: list = []
    current: list = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                current.append("")
            continue
        is_top_level = line and line[0] not in (" ", "\t")
        starts_new = is_top_level and (
            stripped.startswith(("def ", "async def ", "class ", "@"))
            or re.match(
                r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*\s*=(?!=)",
                stripped,
            )
        )
        if starts_new and current:
            while current and not current[-1]:
                current.pop()
            stmts.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        while current and not current[-1]:
            current.pop()
        stmts.append("\n".join(current))

    # Merge leading-comment-only chunks into the next real statement.
    merged: list = []
    pending: list = []
    for s in stmts:
        is_comment_only = all(
            not ln.strip()
            or ln.lstrip().startswith(("#", "//", "/*", "*"))
            for ln in s.split("\n")
        )
        if is_comment_only:
            pending.append(s)
            continue
        if pending:
            merged.append("\n".join(pending) + "\n" + s)
            pending = []
        else:
            merged.append(s)
    if pending:
        if merged:
            merged[-1] += "\n" + "\n".join(pending)
        else:
            merged = pending

    # If everything is comment-only, no real statements.
    if not merged:
        return []
    has_real = any(
        not all(
            not ln.strip()
            or ln.lstrip().startswith(("#", "//", "/*", "*"))
            for ln in s.split("\n")
        )
        for s in merged
    )
    if not has_real:
        return []
    return merged


def _try_multi_statement_apply(base, body, path,
                                prior_insertions=None,
                                prior_scopes=None):
    """Apply a body that contains two or more adjacent statements.

    See module docstring, MULTI-STATEMENT APPLY section.
    """
    global _in_multi_statement
    if _in_multi_statement:
        return None
    if not path.endswith(".py"):
        return None
    stmts = _split_python_statements(body)
    if len(stmts) < 2:
        return None

    current = base
    _in_multi_statement = True
    try:
        for stmt in stmts:
            if not stmt.strip():
                continue
            stmt_patch = {
                "body": stmt,
                "context_before": "",
                "fence_lang": "py",
            }
            result = _try_deterministic_patch_inner(
                current, stmt_patch, path,
                prior_insertions=prior_insertions,
                prior_scopes=prior_scopes,
            )
            if result is None:
                return None
            current = result
    finally:
        _in_multi_statement = False

    return current if current != base else None


# ===========================================================================
#  Indented def/class replace (2026-10-28g)
# ===========================================================================

def _try_indented_def_replace(base: str, body: str):
    """Replace an *indented* `def name(...)` / `async def` / `class`
    declaration in base with the body's version.

    `body` MUST preserve its original indentation.  Pass `raw_body`
    from the caller — NOT `body_stripped`.  See the module docstring,
    INDENTED-DEF REINDENT BUG section, for why `body_stripped` breaks
    the internal reindent computation.

    See also module docstring, INDENTED DEF/CLASS REPLACE section.
    """
    from .validate_output import _js_balance

    m = re.match(
        r"^(?:@\w[\w.]*\s*\n)*\s*(?:async\s+)?(?:def|class)\s+(\w+)",
        body,
    )
    if not m:
        return None
    name = m.group(1)

    # Top-level declaration with the same name?  Leave it to scoped-
    # replace.
    if re.search(
            rf"^(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b",
            base, re.MULTILINE):
        return None

    # Structural sanity.
    b, p, k, u = _js_balance(body)
    if b != 0 or p != 0 or k != 0 or u:
        return None

    # Find indented matches.
    base_lines = base.split("\n")
    indented_re = re.compile(
        rf"^([ \t]+)(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b"
    )
    candidates = []
    for i, line in enumerate(base_lines):
        m2 = indented_re.match(line)
        if not m2:
            continue
        candidates.append((i, len(m2.group(1))))

    if len(candidates) != 1:
        return None

    start, indent_len = candidates[0]
    end = start + 1
    for j in range(start + 1, len(base_lines)):
        line = base_lines[j]
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= indent_len:
            end = j
            break
        end = j + 1

    indent_str = " " * indent_len
    reindented = _reindent_body(indent_str, body)
    new_lines = base_lines[:start] + reindented.split("\n") + base_lines[end:]
    result = "\n".join(new_lines)
    return result if result != base else None


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

    Refuses when the body is not *only* an import statement — a body
    like `import { foo } from "x";\n\nconst bar = ...` would
    otherwise replace the base's import line with the whole body,
    misplacing the trailing content at the import's position.

    Returns `base` (not None) when the base already has an identical
    import — the outer loop then marks the patch `already-present`.
    """
    joined = _join_continuations(raw_body)
    m = _IMPORT_JOIN_RE.search(joined)
    if not m:
        return None
    # Refuse multi-line bodies that are not pure imports.
    body_stripped = joined.strip()
    tail = body_stripped[m.end():].strip() if m.end() < len(body_stripped) else ""
    if tail:
        return None
    module = m.group(2)

    for line in base.split("\n"):
        stripped = line.strip()
        lm = _IMPORT_JOIN_RE.match(stripped)
        if not lm:
            continue
        if lm.group(2) != module:
            continue
        # NEW: already-present short-circuit for identical imports.
        if stripped == body_stripped:
            return base
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
#  Already-present pre-check helpers (2026-10-28f)
# ===========================================================================

def _is_already_applied(body_stripped, base):
    """True iff the body is already present in base well enough that
    applying it again would duplicate content.

    Two tiers (see patch_already.py module docstring and the module-
    level ALREADY-PRESENT PRE-CHECK section above):

      · Short bodies (< 3 lines): strict contiguous-window match.
        Token fallback would false-positive on single-line import
        changes.

      · Multi-line bodies (>= 3 lines): require a long contiguous
        run AND token presence.  The contiguity gate is what
        distinguishes a reprint (content genuinely already there)
        from a new block whose tokens happen to be dispersed in
        base (comfyui.py idx=97: a try/except that introduces
        `output_rotation` — new tokens — but whose remaining
        vocabulary is otherwise common in base).
    """
    body_lines = [ln for ln in body_stripped.splitlines() if ln.strip()]
    if len(body_lines) < 3:
        return _exact_lines_present(body_stripped, base)
    run, total = _max_contiguous_run(body_stripped, base)
    if total == 0:
        return False
    if run / total < 0.95:
        return False
    return _patch_already_present(body_stripped, base)


# ===========================================================================
#  Dispatcher
# ===========================================================================

def _try_deterministic_patch(base, patch, path="",
                              prior_insertions=None, prior_scopes=None):
    """Crash-isolated entry point.

    `prior_scopes` is an optional `dict[stripped_line -> scope_name]`
    that disambiguates a repeated prior-insertion anchor.  See
    `apply_patches._record_single_line_anchor` and the module
    docstring, Prior-insertions threading section.
    """
    try:
        return _try_deterministic_patch_inner(
            base, patch, path,
            prior_insertions=prior_insertions,
            prior_scopes=prior_scopes,
        )
    except Exception as e:
        import sys
        print(f"  [patch] deterministic attempt raised: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def _try_deterministic_patch_inner(base, patch, path="",
                                    prior_insertions=None,
                                    prior_scopes=None):
    raw_body = patch["body"]
    body_stripped = raw_body.strip()
    if not body_stripped:
        return None

    context_before = patch.get("context_before", "")

    # Signature-only replacement.  Runs BEFORE the already-present
    # pre-check: a signature-only body is a fragment whose body is
    # in base, so the pre-check would false-positive on it.
    signature_replace = _try_signature_replace(base, raw_body)
    if signature_replace is not None:
        return signature_replace

    # NEW (2026-10-28f): already-present pre-check.
    # Skipped for deletion instructions and diff bodies — their bodies
    # are "content to disappear" and "content to transform", not
    # "content to insert".  Both have their own already-applied
    # predicates downstream (`_deletion_already_applied`,
    # `_diff_already_applied`).  Firing `_is_already_applied` on them
    # would treat the content-to-delete as content-already-present and
    # skip the deletion entirely — the exact cause of the test_07
    # regression.
    #
    # NOTE (2026-11-01): `_is_deletion_instruction` was narrowed
    # (core.py) so prose like "Delete-and-recreate the layer" no
    # longer fires it.  Before that narrowing, the comfyui.py
    # `comfy_set_layer_pixels` patch was marked deletion, which
    # bypassed the pre-check entirely; the misclassification happened
    # elsewhere.  Post-narrowing, the pre-check runs on that patch
    # and correctly identifies it as not-already-present.  Pass 8.7d
    # (consistency.py) is the backstop for any remaining false
    # positives that DO slip through.
    _is_deletion = _is_deletion_instruction(raw_body, context_before)
    _is_diff = _looks_like_diff_body(raw_body)
    if not _is_deletion and not _is_diff:
        if _is_already_applied(body_stripped, base):
            return base

    # NEW (2026-10-28g): multi-statement apply.  Fires for bodies that
    # split into 2+ top-level statements.  Runs BEFORE the placeholder
    # gate so sub-statements that individually carry placeholders get
    # their normal handling.  See module docstring, MULTI-STATEMENT
    # APPLY section.
    if (not _in_multi_statement
            and not _is_deletion
            and not _is_diff):
        _ms_result = _try_multi_statement_apply(
            base, body_stripped, path,
            prior_insertions=prior_insertions,
            prior_scopes=prior_scopes,
        )
        if _ms_result is not None:
            return _ms_result

    # Placeholder gate.  See module docstring.
    if _has_placeholder_marker(raw_body):
        scope_name, _kind = _extract_scope_from_body(body_stripped)
        if scope_name:
            merged = _try_merge_placeholder_in_scope(
                base, scope_name, raw_body)
            if merged is not None and not _has_placeholder_marker(merged):
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

    if _is_deletion:
        deleted = _delete_exact_block(base, raw_body)
        if deleted is not None:
            return deleted

    if _is_diff:
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

    # NEW (2026-10-28g): indented def/class replace.  A nested
    # `def name(...)` — e.g. `def on_progress(...)` inside
    # `handle_inpaint` — is not reachable by scoped-replace (which
    # requires column-0 declarations) and not by any insert heuristic
    # (which would put the new function *inside* the enclosing
    # function, duplicating the name).  See module docstring,
    # INDENTED DEF/CLASS REPLACE section.
    #
    # FIX (2026-11-01): pass raw_body, NOT body_stripped.  See module
    # docstring, INDENTED-DEF REINDENT BUG section.  body_stripped
    # zeroes the first line's indent, which makes `_reindent_body`
    # compute a wrong `orig_indent_len` and over-indent every
    # subsequent line by the fence's original indent.
    if body_stripped.startswith(("def ", "async def ", "class ")):
        _indented_result = _try_indented_def_replace(base, raw_body)
        if _indented_result is not None:
            return _indented_result

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
            prior_scopes=prior_scopes,
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
            # NEW (2026-10-28g, refined 2026-10-28h): refuse when the
            # body has substantial *trailing content* after the object
            # literal.  A body like
            #   x = { ... }
            #   def foo(): ...
            # is NOT a pure object-literal splice — the splice would
            # silently drop the def (the historical bug that lost
            # Fix 3's on_progress).
            #
            # A bare trailing `;` is the JS/C-family statement
            # terminator, not trailing content.  The initial version
            # of this refusal treated `export const foo = {...};` as
            # trailing content and regressed fixtures 02, 04, and 35.
            # The tail check strips a single leading `;` before
            # checking for non-comment content, so:
            #   x = {...}                       → splice fires
            #   x = {...};                      → splice fires
            #   x = {...};  # note              → splice fires
            #   x = {...};\ndef foo(): ...      → refused
            #   x = {...}\ndef foo(): ...       → refused
            _patch_open = body_stripped.index("{", m.start())
            _is_py = _looks_like_python_fragment(body_stripped)
            _patch_close = _find_matching_brace(
                body_stripped, _patch_open, is_python=_is_py)
            if _patch_close > 0:
                _tail = body_stripped[_patch_close + 1:]
                _tail_stripped = _tail.lstrip()
                if _tail_stripped.startswith(";"):
                    _tail_stripped = _tail_stripped[1:].lstrip()
                _trailing_lines = _tail_stripped.split("\n")
                _non_comment = [
                    ln for ln in _trailing_lines
                    if ln.strip() and not ln.lstrip().startswith(
                        ("#", "//", "/*", "*"))
                ]
                if _non_comment:
                    return None
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
        prior_scopes=prior_scopes,
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
