"""
patch_dispatch — the deterministic-patch dispatcher.

`_try_deterministic_patch` is the single entry point the pass-6 main
loop calls for each patch.  It dispatches, in order:

   0. signature-only replacement
   0a. already-present pre-check
   0b. multi-statement apply
   1. placeholder-marker gate
   2. Python-completeness gate
   3. deletions
   4. raw diffs
   5. import replaces
   6. single-line replaces
   6a. indented def/class replaces
   6b. decorated-def scope insert (2026-11-02)
   7. scoped replaces
   8. ellipsis splices, with fallback
   9. object-literal splices (with trailing-content refusal)
   9a. region replaces (2026-10-29) — see below
  10. named-dict inserts
  11. context inserts
  12. object-entry inserts
  13. insert-into-named-scope
  14. insert-before-anchor
  15. insert-between-anchors
  16. insert-after-anchor
  17. AST anchor
  18. structural append

AST-SNAPPED REGION-REPLACE (2026-10-29)
=======================================
`_try_region_replace` now splices via `patch_bounds.splice_region`
instead of the raw `"\n".join(base_lines[:start] + body + base_lines[end:])`.
The candidate set is:

  1. The unmodified `[start, end)` from the similarity sweep.
  2. AST-snapped boundaries — expanded outward to the enclosing
     top-level statements (`ast` for `.py`, tree-sitter for `.js`/
     `.ts`/etc. and for the dominant embedded JS string in a `.py`).
  3. A ±3-line sweep around the original boundaries as fallback.

Each candidate goes through `splice_region`, which validates
structurally.  The first valid splice wins.  When none produces
valid content, `_try_region_replace` returns None and the caller
falls through to the insert heuristics (which may fail-closed
cleanly) or to fail-closed at the record level.

This closes the class of failure that produced invalid embedded JS
in `pg_panel_scrubs.py` and `pg_panel_bindings.py` on 2026-10-28:
a line-based window that stopped one line short of the closing `}`,
leaving a stray `}` as an orphan, which `validate_content` caught
too late (in pass 8, not at heuristic time).

SIZE-AWARE SIMILARITY (2026-11-05)
==================================
`_try_region_replace` used a flat `min_ratio=0.55` gate before
running the splice.  For a patch whose body is *larger* than the
base region it replaces (expand-shaped edit — the
`pg_base.py` rotation widget goes from 10 lines to 17), the ratio
is structurally penalised: `SequenceMatcher.ratio() = 2*M / (len(A) + len(B))`,
so a 7-line match against a 10-line base and 17-line body scores
`14/27 ≈ 0.52` — under the gate, even though the actual content is
7 lines of solid match.

The 2026-11-05 fix computes an `effective_min_ratio` per candidate
window, scaled down by the size ratio between window and body:

    size_ratio = min(w_size, body_size) / max(w_size, body_size)
    effective_min_ratio = min_ratio * size_ratio

For the rotation-widget case, `size_ratio = 10/17 ≈ 0.59`, so
`effective_min_ratio ≈ 0.32`, and the 0.52 actual ratio passes.
The structural-validity gate inside `splice_region` is unchanged,
so a bad splice from a wrong window is still rejected.

PROSE-NAMED-SCOPE: INSERT vs REPLACE (2026-11-05)
=================================================
`_prose_names_scope` gates region-replace: when the prose says
"in `X`", the region-replace path is refused so that
`_try_insert_into_named_scope` runs instead.

Two bugs in the prior version:

  1. The regex `\b(?:in|inside|within|to|for)\s+`[A-Za-z_][\w$]*``
     did NOT match dotted identifiers or filenames (`pg_base.py`,
     `comfyui_krita.py`).  When the prose said "In `pg_base.py`,
     replace the rotation field:", the regex missed it and
     region-replace was NOT gated — actually the right outcome,
     but by accident.

  2. More importantly: the gate fired for *replace* prose too.
     "In `handle_inpaint`, replace the mask pipeline with:" was
     gated as if it were an insert, and `_try_insert_into_named_
     scope` inserted rather than replacing.  The result was a
     scope content doubled (once in the base, once inserted).

The 2026-11-05 fix distinguishes the two: prose that both names a
scope AND contains a replace-family verb is *replace intent*, and
region-replace is NOT gated (region-replace is exactly the path
that should fire).  Only prose that names a scope without a replace
verb — the "insert into scope" shape — is gated.

The regex also now accepts dots in the backticked identifier, so
"In `pg_base.py`, ..." is correctly recognised as naming a scope
(and, since it also says "replace", region-replace runs).

Prior docstring content (unchanged):
- MULTI-STATEMENT APPLY (2026-10-28g)
- INDENTED DEF/CLASS REPLACE (2026-10-28g)
- INDENTED-DEF REINDENT BUG (2026-11-01)
- MULTI-STATEMENT DEDENT (2026-11-01b)
- DECORATOR MERGE (2026-11-02)
- LANGUAGE-AWARE STRUCTURAL CHECK (2026-11-01b)
- DECORATED-SCOPE ROUTING (2026-11-02)
- REGION REPLACE (2026-10-29)
"""

import difflib
import re
import textwrap
from typing import Optional

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


_in_multi_statement = False


# ============================================================================
#  Prose-scope detection (used to gate region-replace)
# ============================================================================
#
# Every shape below is a way for the surrounding prose to say "put
# this inside scope X".  When any of them match AND the prose does
# not carry a replace-family verb, `_try_insert_into_named_scope` is
# the correct handler and region-replace must not compete — the
# prose has already named the destination as an *insertion* target.
#
# FIX (2026-11-05): the backticked-identifier character class now
# accepts dots and slashes, so `pg_base.py` and
# `playgrounds/inpaint/pg_base.py` are recognised.  A prose line like
# "In `pg_base.py`, replace the rotation field:" is recognised as
# scope-naming (backtick-in-scope).  Whether region-replace is then
# gated depends on whether a replace verb is also present — see
# `_prose_names_scope`.
_PROSE_NAMES_SCOPE_RE = re.compile(
    r'\b(?:in|inside|within|to|for)\s+`[A-Za-z_][\w$./]*`'
    r'|\bfind\s+the\s+`[A-Za-z_][\w$./]*`'
    r'|\bthe\s+`[A-Za-z_][\w$./]*`\s+'
    r'(?:command|method|function|class|handler|endpoint|'
    r'decorator|implementation)\b'
    r'|\badd\s+(?:the\s+following\s+)?(?:method|function|command|'
    r'class)\s+to\s+`[A-Za-z_][\w$./]*`',
    re.IGNORECASE,
)


# Replace-family verbs.  Presence of any of these in the prose turns
# a scope-naming into a *replace* intent, and region-replace is the
# correct path — it must not be gated.
_PROSE_REPLACE_SCOPE_RE = re.compile(
    r'\b(?:replace|change|update|rewrite|modify|substitute|swap)\b',
    re.IGNORECASE,
)


def _prose_names_scope(prose: str) -> bool:
    """True iff the prose names a target scope AND the intent is
    *insertion into* that scope, not *replacement of* it.

    A prose line like "In `handle_inpaint`, add a try/except around
    the payload reads:" names a scope and says "add" — insertion.
    Region-replace must not fire.

    A prose line like "In `pg_base.py`, replace the rotation field:"
    names a scope and says "replace" — replacement.  Region-replace
    is exactly the handler that should fire, and it must not be
    gated by the scope-name check.

    See the module docstring, PROSE-NAMED-SCOPE: INSERT vs REPLACE.
    """
    if not prose:
        return False
    if not _PROSE_NAMES_SCOPE_RE.search(prose):
        return False
    if _PROSE_REPLACE_SCOPE_RE.search(prose):
        return False   # it's a replace; let region-replace run
    return True


def _split_python_statements(body: str) -> list:
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
            last_non_blank = ""
            for ln in reversed(current):
                if ln.strip():
                    last_non_blank = ln
                    break
            if last_non_blank.lstrip().startswith("@"):
                current.append(line)
                continue
            while current and not current[-1]:
                current.pop()
            stmts.append("\n".join(current))
            current = []
        current.append(line)

    if current:
        while current and not current[-1]:
            current.pop()
        stmts.append("\n".join(current))

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


def _try_indented_def_replace(base: str, body: str, path: str = ""):
    from .structural import check_balance

    m = re.match(
        r"^(?:@\w[\w.]*\s*\n)*\s*(?:async\s+)?(?:def|class)\s+(\w+)",
        body,
    )
    if not m:
        return None
    name = m.group(1)

    if re.search(
            rf"^(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b",
            base, re.MULTILINE):
        return None

    balance_err = check_balance(body, path)
    if balance_err is not None:
        return None

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


_IMPORT_JOIN_RE = re.compile(
    r'import\s+\{([^}]*)\}\s*from\s*["\']([^"\']+)["\']\s*;?',
    re.DOTALL,
)

_CONTINUATION_ENDINGS = (",", "{", "(", "\\")


def _join_continuations(body: str) -> str:
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
    joined = _join_continuations(raw_body)
    m = _IMPORT_JOIN_RE.search(joined)
    if not m:
        return None
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
        if stripped == body_stripped:
            return base
        indent = line[:len(line) - len(line.lstrip())]
        reindented = _reindent_body(indent, raw_body.strip())
        result = base.replace(line, reindented, 1)
        return result if result != base else None
    return None


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


def _is_already_applied(body_stripped, base):
    body_lines = [ln for ln in body_stripped.splitlines() if ln.strip()]
    if len(body_lines) < 3:
        return _exact_lines_present(body_stripped, base)
    run, total = _max_contiguous_run(body_stripped, base)
    if total == 0:
        return False
    if run / total < 0.95:
        return False
    return _patch_already_present(body_stripped, base)


# ============================================================================
#  Region replace (2026-10-29; size-aware 2026-11-05)
# ============================================================================

def _try_region_replace(base: str, body_stripped: str,
                        path: str = "",
                        context_before: str = "",
                        min_ratio: float = 0.55,
                        min_overlap: float = 0.4,
                        ) -> Optional[str]:
    """Replace a region of `base` with `body_stripped` when the body
    is a modified version of that region.

    The similarity sweep selects the best-scoring window (score =
    overlap_fraction + ratio), then `splice_region` tries the raw
    boundaries, the AST-snapped boundaries, and a ±3-line sweep.
    Each candidate is validated structurally; the first valid
    splice wins.

    Gated on `context_before`: when the surrounding prose names a
    scope as an *insertion target* (see `_prose_names_scope`), this
    returns None so `_try_insert_into_named_scope` can place the
    body without region-replace overriding it.  When the prose
    names a scope as a *replacement target* (e.g. "In `pg_base.py`,
    replace ..."), region-replace is not gated — it is the correct
    handler for that shape.

    Per-window similarity threshold (2026-11-05): the flat
    `min_ratio` is scaled down by the size ratio between window and
    body, so expand-shaped edits (body larger than base region) are
    not structurally penalised.  See the module docstring,
    SIZE-AWARE SIMILARITY.
    """
    if not body_stripped or not base:
        return None

    if _prose_names_scope(context_before):
        return None

    body_lines = body_stripped.splitlines()
    if len(body_lines) < 3:
        return None

    if body_stripped in base:
        return None

    base_lines = base.splitlines()
    if not base_lines:
        return None

    def _stripped_nonblank(lines):
        return [ln.strip() for ln in lines if ln.strip()]

    body_stripped_list = _stripped_nonblank(body_lines)
    if len(body_stripped_list) < 3:
        return None

    body_set = set(body_stripped_list)
    first_body = body_stripped_list[0]
    first_body_word = first_body.split(None, 1)[0] if first_body else ""

    best: Optional[tuple] = None
    for factor in (0.3, 0.4, 0.5, 0.6, 0.7, 0.85, 1.0,
                   1.15, 1.3, 1.5, 1.8, 2.2):
        window_size = max(3, int(len(body_lines) * factor))
        if window_size > len(base_lines):
            continue
        # Size-aware threshold: a window that is much smaller or much
        # larger than the body structurally caps the achievable
        # SequenceMatcher ratio.  Scale the minimum down by the size
        # ratio so expand-shrink edits are not rejected on ratio
        # alone.  The structural validation inside splice_region
        # still rejects bad boundaries.
        size_ratio = min(window_size, len(body_lines)) / max(
            window_size, len(body_lines)
        )
        effective_min_ratio = min_ratio * size_ratio
        for start in range(len(base_lines) - window_size + 1):
            window = base_lines[start:start + window_size]
            window_stripped_list = _stripped_nonblank(window)
            if len(window_stripped_list) < 3:
                continue

            first_window = window_stripped_list[0]
            first_window_word = (
                first_window.split(None, 1)[0] if first_window else ""
            )
            if first_window_word != first_body_word:
                continue

            window_set = set(window_stripped_list)
            if not window_set:
                continue
            overlap = len(window_set & body_set)
            if overlap / len(window_set) < min_overlap:
                continue

            sm = difflib.SequenceMatcher(
                None, window_stripped_list, body_stripped_list,
                autojunk=False,
            )
            ratio = sm.ratio()
            if ratio < effective_min_ratio:
                continue

            score = (overlap / len(window_set)) + ratio
            if best is None or score > best[0]:
                best = (score, start, start + window_size, ratio)

    if best is None:
        return None

    _score, start, end, _ratio = best

    from .patch_bounds import (
        try_boundary_pairs, try_ast_snapped_boundaries, splice_region,
    )
    total = len(base_lines)

    candidates = [(start, end)]
    snapped = try_ast_snapped_boundaries(base, path, start, end)
    if snapped is not None and snapped != (start, end):
        candidates.append(snapped)
    candidates.extend(try_boundary_pairs(start, end, total))

    for trial_start, trial_end in candidates:
        result = splice_region(
            base, trial_start, trial_end, body_lines, path)
        if result is not None:
            return result
    return None


def _try_deterministic_patch(base, patch, path="",
                              prior_insertions=None, prior_scopes=None):
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
    body_stripped = textwrap.dedent(raw_body).strip()
    if not body_stripped:
        return None

    context_before = patch.get("context_before", "")

    signature_replace = _try_signature_replace(base, raw_body)
    if signature_replace is not None:
        return signature_replace

    _is_deletion = _is_deletion_instruction(raw_body, context_before)
    _is_diff = _looks_like_diff_body(raw_body)
    if not _is_deletion and not _is_diff:
        if _is_already_applied(body_stripped, base):
            return base

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

    if body_stripped.startswith(("def ", "async def ", "class ")):
        _indented_result = _try_indented_def_replace(
            base, raw_body, path=path)
        if _indented_result is not None:
            return _indented_result

    _first_stripped = next(
        (ln.lstrip() for ln in raw_body.splitlines() if ln.strip()), "")
    if _first_stripped.startswith("@"):
        _decorated_scope_result = _try_insert_into_named_scope(
            base, raw_body, context_before, path)
        if _decorated_scope_result is not None:
            return _decorated_scope_result

    _body_is_obj_lit = bool(
        _OBJECT_LITERAL_ASSIGNMENT_RE.match(body_stripped))
    if _body_is_obj_lit:
        _scope_name, _scope_kind = None, None
    else:
        _scope_name, _scope_kind = _extract_scope_from_body(body_stripped)
    if _scope_name and _scope_in_base(base, _scope_name, _scope_kind):
        _scope_result = _apply_scoped(
            base, _scope_name, body_stripped, path=path)
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

    _region_result = _try_region_replace(
        base, body_stripped, path, context_before)
    if _region_result is not None:
        patch["region_replace_evidence"] = (
            "opening-word match + bidirectional line overlap"
        )
        return _region_result

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


def attribute_sanity_failure(rec, blocks_by_idx, validate_fn=None):
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
