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
   6b. decorated-def scope insert (2026-11-02) — see below
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

MULTI-STATEMENT APPLY (2026-10-28g)
===================================
A body that contains two or more adjacent top-level statements can't
be handled by any single heuristic.  `_try_multi_statement_apply`
splits the body into individual top-level statements and applies each
one through the normal dispatcher.  A sub-statement that fails to
place causes the whole multi-statement attempt to bail (fail-closed).

Recursion is prevented by the module-level `_in_multi_statement`
flag.  Assumes single-threaded execution.

INDENTED DEF/CLASS REPLACE (2026-10-28g)
========================================
`_try_indented_def_replace` finds the unique indented match of a
`def name(...)` in base and replaces it in place.  Fires only when
the name has no column-0 declaration (scoped-replace owns the
top-level case) and exactly one indented match exists.

INDENTED-DEF REINDENT BUG (2026-11-01)
======================================
`body_stripped` used to be `raw_body.strip()`, which only removed
the first line's indent.  Passing `raw_body` (with original indent)
to `_try_indented_def_replace` fixes `_reindent_body`'s
`orig_indent_len` computation.

MULTI-STATEMENT DEDENT (2026-11-01b)
====================================
`_try_deterministic_patch_inner` calls
`textwrap.dedent(raw_body).strip()` for `body_stripped`.  The prior
`.strip()` only removed leading whitespace from the first line, so
a body indented at 4 spaces on every line produced a hybrid where
the first line was at column 0 and subsequent lines were at 8
spaces.  `_split_python_statements` then saw no top-level split
point.

`textwrap.dedent` removes the *common* leading whitespace across
all lines.  `inpaint_server.py`'s motivating shape — `captured =
{...}` followed by `def on_progress(...)` — now splits into two
statements cleanly.

DECORATOR MERGE (2026-11-02)
============================
`_split_python_statements` now merges a `@decorator` line with the
`def`/`class` line that follows it.  Without this, a body like
`@command(name="new")\ndef new_cmd(...)` was split into TWO
statements: a bare decorator (which no heuristic can place) and the
def.  Multi-statement apply would attempt the decorator first, it
would fail (as it should — a bare decorator has no meaning), and
the whole multi-statement attempt would bail.  The intended
dispatch (scope-insert into the prose-named class) was never
reached.

With the merge, `_split_python_statements` returns ONE statement
for a decorator + def/class unit.  Multi-statement doesn't fire;
`_try_insert_into_named_scope` receives the decorated body intact
and places it correctly.

LANGUAGE-AWARE STRUCTURAL CHECK (2026-11-01b)
=============================================
`_try_indented_def_replace`'s structural gate now calls
`structural.check_balance(body, path)` instead of `_js_balance`.
`.py` destinations route through `ast.parse`; JS bodies fall back
to `_js_balance`.

DECORATED-SCOPE ROUTING (2026-11-02)
====================================
A body whose first non-blank line starts with `@` is a decorated
def/class.  Such bodies MUST be placed inside their prose-named
scope (a class or module); the `named_dict`, `context`, and
`object_entry` heuristics do not know how to place a decorated
definition and will misplace it (context-insert inserts at the
anchor line's indent — 8 spaces inside a method — producing a
nested def with wrong semantics).

New in the chain: before those heuristics, if the body starts with
`@`, try `_try_insert_into_named_scope` directly.  This is a narrow
routing that only affects decorated bodies.

REGION REPLACE (2026-10-29)
===========================
The class of edit that neither the specific replacement heuristics
(scoped, object-literal, signature, indented-def) nor the insert
family handles correctly: a body that is a *modified version* of an
existing multi-declaration region but does not declare a scope and
is not a pure object-literal assignment.

Canonical shape (from the 2026-10-28 run):

    base:
        const rotationEl = document.getElementById("viewRotation");
        const rotationValueEl = document.getElementById("viewRotationValue");

        if (rotationEl) { rotationEl.addEventListener(...); }

        pgkitRegisterField("view.rotation", { ... });

    body:
        const rotationEl = document.getElementById("viewRotation");
        const rotationInputEl = document.getElementById("viewRotationInput");
        const rotationValueEl = document.getElementById("viewRotationValue");

        if (rotationEl) { rotationEl.addEventListener(...); }
        if (rotationInputEl) { rotationInputEl.addEventListener(...); }

        pgkitRegisterField("view.rotation", { ... });

Before this heuristic: every replacement path returns None (no
scope to replace, not a `def`/`class` signature, not an object-
literal assignment), the body falls through to `_try_structural_append`
or `_ast_insert`, and the resulting file has both the old region
AND the new region — duplicate `const` declarations, duplicate
`if` blocks, dead code.

Region-replace fires when:
  · The body's first non-blank line has the same opening word as
    the candidate base window (e.g. `const` matches `const`).
  · At least 40% of the window's stripped lines appear verbatim in
    the body.
  · The window and body are at least 55% line-similar under
    `SequenceMatcher`.
  · AND the surrounding prose does NOT name a scope ("in `X`",
    "to `X`", "find the `X` command", etc.).  When prose names a
    scope, `_try_insert_into_named_scope` is the correct route —
    the target is explicit and region-replace must not override
    it.  The prose gate is what keeps `test_40` (try/except
    inside a named function) correct: without it, region-replace
    would fire on the try/except body *before* insert-into-scope
    runs, replacing the wrong block.

The two overlap measures reject the failure modes that would
otherwise let region-replace clobber real code:

  · Pure insertion — body's `if (condC) { doC(); }` shares `if`
    and `}` with base's `if (condA) { doA(); doA2(); }`, but the
    verbatim-overlap is zero.  Rejected by the 40% check.

  · Unrelated same-construct — body's rewritten `function foo()`
    is entirely new content but starts with `function`.  The
    overlap is 2/4 = 50% (matches `function foo() {` and `}`
    only) but the similarity is 44% — under 55%.  Rejected by the
    similarity check.

Region-replace sits after scoped-replace in the chain so it never
overrides a more specific replacement.  It sits before the insert
family so a modified region *replaces* rather than being appended
after itself.

A tree-sitter based variant (matching by declaration name instead
of line-similarity) can be added later as `recover/js_scope.py`
once a `js_scope_index` module exists; the text heuristic above is
sufficient for the shapes the 2026-10-28 run produced.  The two
SKIP cases in `tests/recover/test_region_replace.py` are that
future-work marker.
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
# this inside scope X".  When any of them match, `_try_insert_into_
# named_scope` is the correct handler and region-replace must not
# compete — the prose has already named the destination.
#
# Kept as a compiled set of alternatives rather than reaching into
# `patch_scope_insert._extract_scope_name_from_prose` so this gate
# stays local: if the scope extractor changes its patterns, this
# gate can still answer the simpler "does prose name any scope?"
# question without breaking.
_PROSE_NAMES_SCOPE_RE = re.compile(
    r'\b(?:in|inside|within|to|for)\s+`[A-Za-z_][\w$]*`'
    r'|\bfind\s+the\s+`[A-Za-z_][\w$]*`'
    r'|\bthe\s+`[A-Za-z_][\w$]*`\s+'
    r'(?:command|method|function|class|handler|endpoint|'
    r'decorator|implementation)\b'
    r'|\badd\s+(?:the\s+following\s+)?(?:method|function|command|'
    r'class)\s+to\s+`[A-Za-z_][\w$]*`',
    re.IGNORECASE,
)


def _prose_names_scope(prose: str) -> bool:
    """True iff the prose names a scope for this block to go into.

    Prose that names a scope is an instruction: the block belongs
    inside that scope.  Insert-into-scope handles it; region-replace
    must not run — a body that *could* look like a replacement of
    some existing region (e.g. a `try:`/`except:` fragment under a
    function whose body already has one) is being inserted as a
    second sibling, not as a rewrite.

    The motivating shape that motivated the gate:
    `test_40`'s try/except body, prose "In `handle_thing`, after
    the existing `payload.get` reads:".  Without the gate,
    region-replace fires on the try/except fragment because it
    structurally resembles the existing try/except block, replacing
    it and losing the `timeout` bindings the function still returns.
    """
    if not prose:
        return False
    return bool(_PROSE_NAMES_SCOPE_RE.search(prose))


def _split_python_statements(body: str) -> list:
    """Split Python source into top-level statements.

    Refuses bodies containing triple-quoted strings: the regex-based
    splitter can't tell a top-level `def` inside a string literal from
    a real one.

    DECORATOR MERGE (2026-11-02): a `@decorator` line followed by a
    `def`/`class` line is ONE statement (the decorator belongs with
    its def).  Splitting them leaves the decorator orphaned, which
    no heuristic can place.
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
            # Decorator merge: if the previous non-blank line in
            # `current` is a decorator (`@...`), keep the current
            # line attached — whether it's another decorator or the
            # def/class the decorators apply to.
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
    """Replace an *indented* `def name(...)` / `async def` / `class`
    declaration in base with the body's version.

    `body` MUST preserve its original indentation.  Pass `raw_body`
    from the caller.

    `path` routes `structural.check_balance` to `ast.parse` for `.py`
    bodies.
    """
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
#  Region replace (2026-10-29)
# ============================================================================

def _try_region_replace(base: str, body_stripped: str,
                        path: str = "",
                        context_before: str = "",
                        min_ratio: float = 0.55,
                        min_overlap: float = 0.4,
                        ) -> Optional[str]:
    """Replace a region of `base` with `body_stripped` when the body
    is a modified version of that region.

    See the module docstring, REGION REPLACE section, for the
    motivating shape and the acceptance criteria.

    Gated on `context_before`: when the surrounding prose names a
    scope ("in `X`", "to `X`", "find the `X` command", "add the
    following method to `X`", etc.), the block is being inserted
    into a specific scope and `_try_insert_into_named_scope` will
    place it correctly.  Region-replace must not run in that case
    — a fragment that structurally resembles some existing region
    (e.g. a `try:`/`except:` inside a function that already has one)
    is a *second* sibling, not a rewrite.  Without this gate,
    `test_40`'s try/except fragment replaces the existing try/except
    and drops the `timeout` bindings the function still returns.

    Returns the modified content, or None when no region qualifies,
    when the prose names a scope, or when firing would risk
    clobbering unrelated content.
    """
    if not body_stripped or not base:
        return None

    if _prose_names_scope(context_before):
        return None

    body_lines = body_stripped.splitlines()
    if len(body_lines) < 3:
        return None

    # Exact-substring already-applied check: if the whole body is
    # already in base, the caller's `_is_already_applied` should
    # have fired; guard anyway so a direct call does not produce a
    # redundant region-replace.
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
    # The factor sweep covers both directions: bodies that are
    # larger than their base region (typical expansion) and bodies
    # that are smaller than their region (typical contraction).
    # `max(3, …)` keeps a floor so tiny bodies do not produce
    # degenerate one-line windows.
    for factor in (0.3, 0.4, 0.5, 0.6, 0.7, 0.85, 1.0,
                   1.15, 1.3, 1.5, 1.8, 2.2):
        window_size = max(3, int(len(body_lines) * factor))
        if window_size > len(base_lines):
            continue
        for start in range(len(base_lines) - window_size + 1):
            window = base_lines[start:start + window_size]
            window_stripped_list = _stripped_nonblank(window)
            if len(window_stripped_list) < 3:
                continue

            first_window = window_stripped_list[0]
            first_window_word = (
                first_window.split(None, 1)[0] if first_window else ""
            )
            # Same opening construct only.
            if first_window_word != first_body_word:
                continue

            # Criterion 1: verbatim line overlap.
            window_set = set(window_stripped_list)
            if not window_set:
                continue
            overlap = len(window_set & body_set)
            if overlap / len(window_set) < min_overlap:
                continue

            # Criterion 2: line-similarity.
            sm = difflib.SequenceMatcher(
                None, window_stripped_list, body_stripped_list,
                autojunk=False,
            )
            ratio = sm.ratio()
            if ratio < min_ratio:
                continue

            score = (overlap / len(window_set)) + ratio
            if best is None or score > best[0]:
                best = (score, start, start + window_size, ratio)

    if best is None:
        return None

    _score, start, end, _ratio = best

    new_lines = base_lines[:start] + body_lines + base_lines[end:]
    result = "\n".join(new_lines)

    # Preserve base's trailing-newline behaviour.
    if base.endswith("\n") and not result.endswith("\n"):
        result += "\n"

    if result == base:
        return None

    # Aggressive-replace guard: don't lose more than half the file.
    if len(new_lines) < len(base_lines) * 0.5:
        return None

    return result


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

    # NEW (2026-11-02): decorated-def scope routing.
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

    # NEW (2026-10-29): region-replace.  For bodies that are a
    # modified version of an existing base region but don't match
    # any of the more specific replacement heuristics.  Gated on
    # prose not naming a scope (see `_prose_names_scope`): when the
    # prose already tells us where the block goes,
    # `_try_insert_into_named_scope` is the correct route and
    # region-replace must not override it.
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
