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
   6c. decorator kwarg dict splice (2026-11-06)  ← NEW
   7. scoped replaces
   8. ellipsis splices, with fallback
   9. object-literal splices (with trailing-content refusal)
   9a. region replaces (2026-10-29) — with an internal statement-level
       pre-check (2026-10-29b), see below
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
`_try_region_replace_lines` (formerly the body of
`_try_region_replace`) splices via `patch_bounds.splice_region`
instead of the raw `"\n".join(base_lines[:start] + body + base_lines[end:])`.
The candidate set is:

  1. The unmodified `[start, end)` from the similarity sweep.
  2. AST-snapped boundaries — expanded outward to the enclosing
     top-level statements (`ast` for `.py`, tree-sitter for `.js`/
     `.ts`/etc. and for the dominant embedded JS string in a `.py`).
  3. A ±3-line sweep around the original boundaries as fallback.

Each candidate goes through `splice_region`, which validates
structurally.  The first valid splice wins.  When none produces
valid content, the phase returns None and the caller falls through
to the insert heuristics (which may fail-closed cleanly) or to
fail-closed at the record level.

This closes the class of failure that produced invalid embedded JS
in `pg_panel_scrubs.py` and `pg_panel_bindings.py` on 2026-10-28:
a line-based window that stopped one line short of the closing `}`,
leaving a stray `}` as an orphan, which `validate_content` caught
too late (in pass 8, not at heuristic time).

STRUCTURAL REGION-REPLACE (2026-11-05)
=====================================
`_try_region_replace` is now a three-phase dispatcher:

  1. `try_statement_replace` — specific single-statement replacement.
     Handles Return / Assign / AnnAssign / Call bodies by AST-level
     statement signature.  Existed before this change; unchanged.

  2. `_try_region_replace_structural` — NEW.  Multi-statement
     AST-based region match.  Fires when a window of `base`'s
     top-level statements has high AST node-type similarity to
     `body`'s top-level statements *and* shares at least one
     identifier.  Python-only (`.py` / `.pyi`).

     The identifier gate rejects structurally-coincident false
     positives: `foo()` and `bar()` have identical node types but
     no shared identifier, and would otherwise splice `bar()` over
     `foo()`.

     Motivating case: `if flag: do_old()` → `if flag: do_new()`.
     Line-based similarity is 0.5 (1 of 2 lines matches), below the
     0.55 threshold, so line-based declines.  Structural similarity
     is 1.0 (identical node types), and `flag` is shared, so
     structural fires.

  3. `_try_region_replace_lines` — the pre-2026-11-05 line-based
     sweep.  Unchanged.  The fallback for HTML fragments (whose
     content lives inside a Python string literal, invisible to
     `ast`), JS/TS destinations (structural is Python-only), and
     Python bodies whose identifiers are all renamed from the base
     (no shared identifier → structural declines → line-based, which
     also declines, → fail-closed with the target preserved).

  All three phases respect the same `_prose_names_scope` gate at the
  dispatcher: a prose instruction like "in `X`, add:" routes to the
  insert heuristics, not to any replace phase.

STRUCTURAL STATEMENT REPLACE PRE-CHECK (2026-10-29b)
====================================================
Phase 1 (`try_statement_replace`) uses AST-level identity (dict
keys, target names, call arg names) rather than line-level token
overlap, so it correctly discriminates between statements that
share lines but mean different things.

Motivating shape (`inpaint_server.py`, 2026-11-05): a body whose AST
is

    return {"success": False, "message": detail,
            "errors": captured["errors"], "timing": round(elapsed, 3)}

must replace the base's

    return {"success": False, "message": detail,
            "timing": round(elapsed, 3)}

and NOT the base's

    return {"success": True, "image_b64": ..., "timing": ...}

Both base returns share lines (`return {`, `"timing": round(elapsed, 3),`,
`}`) and the *first* one is what the body was written to modify, but
`difflib.SequenceMatcher` on stripped lines scores them similarly — the
`success: True` return scored higher against the body because it had a
matching "message is a name" structure where the `success: False` one
did not.  The `statement_sig`-based matcher uses dict-key identity and
value-exact-match counts, which separate the two cleanly.

Gating order matters: the pre-check fires *inside*
`_try_region_replace`, after the `_prose_names_scope` gate.  The
gate distinguishes "insert into scope X" (prose names X + add-verb)
from "replace in scope X" (prose names X + replace-verb).  Insert
prose must NOT be intercepted by a structural replace; replace prose
MUST be, because the body is a modified statement and the intent is
to replace it, not to append it.  Placing the pre-check before the
gate would defeat the distinction; placing it after preserves it.

Cross-language: `.py` via `ast`, JS/TS via tree-sitter (when the
grammar is available).  When the grammar is missing,
`try_statement_replace` returns None and the similarity sweep runs —
same graceful degradation as `ast_anchor`.

SIZE-AWARE SIMILARITY (2026-11-05)
==================================
`_try_region_replace_lines` used a flat `min_ratio=0.55` gate before
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

SNAP-FIRST CANDIDATE ORDERING (2026-11-05)
==========================================
The line-based phase used to try the RAW similarity window first,
appending the AST-snapped and pair-swept candidates after.  That
worked when the raw window was small enough to be a legitimate
statement, but produced silent corruption on the pg_base.py shape:
the raw 3-line `<input>` window passed all structural checks
(balanced tags, balanced braces), so `splice_region` returned it as
the winner and the AST snap — which would have expanded to the
outer `<div class="field">` — was never consulted.

The 2026-11-05 fix reorders the candidates: snap first, then raw,
then pair-sweep.  Combined with the opening-signature strictness
below, this ensures a body that reopens a wrapper always expands
to the enclosing element rather than being spliced over a
mid-element window.

HTML OPENING-SIGNATURE STRICTNESS (2026-11-05)
==============================================
For `.py`/`.html` destinations where the body's first line starts
with `<`, the line-based similarity sweep requires the window's
first line to match the body's first line *exactly* (after
stripping).  Without this, `<div class="field" style="align-items:center;">`
matched a window opening `<input type="range" id="viewRotation"` —
both start with `<`.  The looser "first word" check is retained for
JS/Python.

DECORATOR KWARG DICT SPLICE (2026-11-06)
========================================
Motivating case: patch 461 in the 2026-10-30 run had body

    args={
        "--timeout": {...},
        "--output_name": {...},
        "--count": {...},
        "--seed": {...},
        "--output_rotation": {...},
    },

targeting the `@command(args={...})` decorator on `comfy_generate`.
The pre-fix dispatcher reached `_try_object_literal_splice`, which
sees `args={...}` at column 0 in the body, finds the base's
`args={...}` inside the decorator, and refuses because the body has
"trailing content after the object literal" (the closing `),` line,
the `def comfy_generate(self, params):` line, etc.).  The patch
landed as `skipped_unplaceable`.

The companion failure was patch 428: body

    "--output_rotation": {"type": "float", "default": 0.0,
                          "help": "..."},

targeting the same `args={...}` dict, inserting a single entry.
`_try_named_dict_insert` routed it to the wrong decorator
(`comfy_connect`'s args) because the prose only said "find the
`comfy_generate` command decorator" and the first `args={...}` in
file order won.

`_try_decorator_kwarg_splice` is the general fix:

  · Parse the body as either `name = {...}` (full-dict replacement)
    or a bare `"key": val,` entry (merge into an existing dict).

  · For the replace case, walk `base`'s AST for a decorated function
    or class whose `@<decorator>` call has a `name={...}` keyword,
    and replace the value's source span with the body's dict text.

  · For the merge case, walk the decorators looking for a kwarg
    dict whose keys do not include the new key, and insert the entry
    before the closing `}`.  If the key is already present in a
    matching decorator, return `base` (already-present no-op).

  · Target selection:
      1. The function named by `_extract_scope_name_from_prose()`.
      2. If that fails, the decorator kwarg dict whose key set has
         the largest overlap with the body's dict keys — must beat
         the runner-up by at least 3 keys, else fail-closed.
      3. If neither applies, decline.

The generalization is that any `@decorator(name={...})` on a
function or class becomes a structural patch target — not just
`@command`.  A future `@route(kwargs={...})` or `@cache(headers={...})`
shape is handled by the same helper.

TOOL-BUG DETECTION (2026-10-29)
===============================
`_try_deterministic_patch` is the entry point for the whole heuristic
chain.  Its historical `except Exception` caught every raise and
returned None, meaning "the heuristics declined this body."  That is
correct for the common case: an `AttributeError` in a heuristic that
mis-parsed a weird body shape, a `ValueError` from text manipulation,
a `SyntaxError` from user-supplied content.

It is WRONG for the case where the heuristic module itself is broken
— a stale import, a renamed function referenced by name, a module
that landed in the wrong directory.  Those are bugs in deeppeek, not
failures of the body, and they abort the entire remaining chain.

`_TOOL_BUG_EXCEPTIONS` marks the types that are code invariants
rather than data conditions.  When the wrapper catches one, it
records `patch["tool_bug"] = "..."` on the patch dict and returns
None (same fail-closed behavior).  Downstream passes now see a
distinguishable signal: `pass6_apply_patches` uses the recorded
exception as `failure_reason` (skipping `_diagnose_patch_failure`,
which would produce an unhelpful "no heuristic matched" message),
adds a "deeppeek bug" warning to `rec.warnings`, and sets
`rec.verification["tool_bug"]`; `pass9_write` surfaces a
`[TOOL_BUG]` line in `warnings.txt`.
"""

import ast
import difflib
import re
import textwrap
from typing import Optional

from ._ast import parse_source
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
    try:
        parse_source(textwrap.dedent(body),
                     filename=path or "<body>")
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
#  Region replace — dispatcher (2026-10-29; three-phase 2026-11-05)
# ============================================================================
#
# Three phases, tried in order:
#
#   1. try_statement_replace   specific single-statement match
#   2. _try_region_replace_structural   generic multi-statement AST match
#   3. _try_region_replace_lines        line-based similarity sweep
#
# All three honour the same _prose_names_scope gate at the top: prose
# that names a scope as an *insertion* target routes to the insert
# heuristics and none of the replace phases fire.
#
# See the module docstring, STRUCTURAL REGION-REPLACE (2026-11-05).

def _try_region_replace(base: str, body_stripped: str,
                        path: str = "",
                        context_before: str = "",
                        min_ratio: float = 0.55,
                        min_overlap: float = 0.4,
                        ) -> Optional[str]:
    """Replace a region of `base` with `body_stripped` when the body
    is a modified version of that region.

    Phase 1: structural statement replace (single statement only).
    Phase 2: structural multi-statement region match (Python only).
    Phase 3: line-based similarity sweep.

    Returns the patched content, or None.  The prose-scope gate
    applies before any phase: an "insert into scope" instruction
    must not be intercepted by a replace handler.
    """
    if not body_stripped or not base:
        return None

    if _prose_names_scope(context_before):
        return None

    # ------------------------------------------------------------------
    #  Phase 1: structural statement replace.
    #
    #  Runs AFTER the prose-scope gate (so insertion intent is not
    #  intercepted) and BEFORE the multi-statement structural matcher
    #  (so a specific signature match pre-empts the generic node-type
    #  match).  When the body isn't a single statement, or the
    #  destination is a language without a signature extractor, or
    #  the match is ambiguous, this returns None and the dispatcher
    #  falls through to phase 2.
    # ------------------------------------------------------------------
    from .patch_statement_replace import try_statement_replace
    stmt_result = try_statement_replace(base, body_stripped, path)
    if stmt_result is not None:
        return stmt_result

    # ------------------------------------------------------------------
    #  Phase 2: structural multi-statement region match.
    #
    #  Python-only.  Declines cleanly when:
    #    · the destination isn't `.py`/`.pyi`
    #    · the body doesn't parse as Python (even after dedent)
    #    · the base doesn't parse as Python
    #    · no candidate window shares an identifier with the body
    #    · the best window's node-type Jaccard is below 0.55
    #    · the best window doesn't beat the runner-up by 0.10
    #
    #  All declines are fail-closed: the dispatcher falls through to
    #  phase 3, which handles the .js / HTML-fragment / renamed-
    #  identifier cases the structural matcher cannot judge.
    # ------------------------------------------------------------------
    structural_result = _try_region_replace_structural(
        base, body_stripped, path)
    if structural_result is not None:
        return structural_result

    # ------------------------------------------------------------------
    #  Phase 3: line-based similarity sweep.
    #
    #  The pre-2026-11-05 heuristic, unchanged.  Handles everything
    #  phase 2 declines.  See the module docstring,
    #  SNAP-FIRST CANDIDATE ORDERING and HTML OPENING-SIGNATURE
    #  STRICTNESS.
    # ------------------------------------------------------------------
    return _try_region_replace_lines(
        base, body_stripped, path,
        min_ratio=min_ratio, min_overlap=min_overlap)


# ============================================================================
#  Region replace — structural phase (2026-11-05)
# ============================================================================

def _try_region_replace_structural(base: str, body_stripped: str,
                                    path: str) -> Optional[str]:
    """Multi-statement AST region match.  Python-only.

    See the module docstring, STRUCTURAL REGION-REPLACE (2026-11-05)
    for the full design rationale.

    Fires when all of:
      · `path` is `.py` or `.pyi`
      · `body_stripped` parses as Python (with dedent fallback)
      · `base` parses as Python
      · a window of `len(body_stmts)` consecutive top-level statements
        in `base` shares at least one identifier with the body
      · node-type multiset Jaccard for the best window is >= 0.55
      · the best window beats the runner-up by >= 0.10

    Returns the spliced content, or None.
    """
    if not path.endswith((".py", ".pyi")):
        return None

    body_tree = _parse_python_body(body_stripped, path)
    if body_tree is None:
        return None

    try:
        base_tree = parse_source(base, filename=path or "<base>")
    except SyntaxError:
        return None

    body_stmts = [n for n in body_tree.body
                  if hasattr(n, "lineno") and hasattr(n, "end_lineno")]
    base_stmts = [n for n in base_tree.body
                  if hasattr(n, "lineno") and hasattr(n, "end_lineno")]

    if not body_stmts or not base_stmts:
        return None

    n = len(body_stmts)
    if n > len(base_stmts):
        return None

    body_ident = [_stmt_identifiers(s) for s in body_stmts]
    body_all = set().union(*body_ident)
    base_ident = [_stmt_identifiers(s) for s in base_stmts]

    scored = []
    for i in range(len(base_stmts) - n + 1):
        window_idents = set().union(*base_ident[i:i + n])
        if not _shares_identifier_sets(body_all, window_idents):
            continue
        window = base_stmts[i:i + n]
        total = sum(_node_type_similarity(b, w)
                    for b, w in zip(body_stmts, window))
        scored.append((total / n, i))

    if not scored:
        return None

    scored.sort(reverse=True)
    best_ratio, best_idx = scored[0]
    if best_ratio < 0.55:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.10:
        return None

    start_line = base_stmts[best_idx].lineno - 1
    end_line = base_stmts[best_idx + n - 1].end_lineno

    from .patch_bounds import (
        try_ast_snapped_boundaries, splice_region,
    )
    snapped = try_ast_snapped_boundaries(
        base, path, start_line, end_line, body_stripped)
    if snapped is not None:
        start_line, end_line = snapped

    out = splice_region(
        base, start_line, end_line,
        body_stripped.split("\n"), path)
    if out.status == "noop":
        return base
    if out.status == "changed":
        return out.content
    return None


def _parse_python_body(body: str, path: str) -> Optional[ast.AST]:
    """Parse a Python body, with dedent fallback for fragments.

    Returns the `ast.Module`, or None.  Motivating case (fixture 40):
    an indented `try: ... except: ...` body parses only after dedent.
    """
    candidates = [body]
    dedented = textwrap.dedent(body)
    if dedented != body:
        candidates.append(dedented)
    for cand in candidates:
        try:
            return parse_source(cand, filename=path or "<body>")
        except SyntaxError:
            continue
    return None


def _stmt_identifiers(stmt) -> set:
    """Set of identifier-shaped tokens in `stmt`: `ast.Name.id` and
    string-constant values.

    The string-constant inclusion catches dict-key literals and
    command-arg names, both of which are load-bearing discriminators
    for the region matcher even though they are not `Name` nodes.
    """
    out: set = set()
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif (isinstance(node, ast.Constant)
              and isinstance(node.value, str)):
            out.add(node.value)
    return out


def _shares_identifier_sets(a: set, b: set) -> bool:
    """True iff `a` and `b` share at least one identifier, or both are
    empty.

    Empty-empty passes because a body with no identifiers (e.g. bare
    `pass` or a small literal) can legitimately match a base window
    with no identifiers — the shape alone is the signal.

    Body-has-identifiers-but-window-has-none (or vice versa) fails:
    it is a structural coincidence, not a modified version.  Motivating
    case: `foo()` and `bar()` have identical node types but share no
    identifier.  Without the gate, the structural matcher would splice
    `bar()` over `foo()`.
    """
    if not a and not b:
        return True
    return bool(a & b)


def _node_type_similarity(a, b) -> float:
    """Jaccard over node-type multisets.  Range [0, 1].

    Shape, not content.  Two `if x: foo()` blocks score 1.0 regardless
    of `x` and `foo`.  The identifier gate (`_shares_identifier_sets`)
    prevents that from being a false positive when they share nothing.

    Multiset, not set: `if a and a:` has two `Name` nodes vs `if a:`
    which has one, so the two do not score 1.0.  This is the
    load-bearing difference from a set-based Jaccard — a body that
    accidentally duplicates an expression is not "identical structure"
    to one that does not.
    """
    from collections import Counter

    def _fp(node):
        return Counter(type(n).__name__ for n in ast.walk(node))

    ca, cb = _fp(a), _fp(b)
    inter = sum((ca & cb).values())
    union = sum((ca | cb).values())
    return inter / union if union else 0.0


# ============================================================================
#  Region replace — line-based phase (2026-10-29)
# ============================================================================
#
# The pre-2026-11-05 heuristic, unchanged.  See the module docstring,
# AST-SNAPPED REGION-REPLACE, SIZE-AWARE SIMILARITY, SNAP-FIRST
# CANDIDATE ORDERING, and HTML OPENING-SIGNATURE STRICTNESS.

def _try_region_replace_lines(base: str, body_stripped: str,
                              path: str = "",
                              min_ratio: float = 0.55,
                              min_overlap: float = 0.4,
                              ) -> Optional[str]:
    """Line-based similarity sweep, with AST-snapped boundaries and
    structural validation.

    Called from `_try_region_replace` after the prose-scope gate,
    `try_statement_replace`, and the structural matcher have all
    declined.  See the parent dispatcher's docstring for the phase
    ordering rationale.
    """
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
    is_html_dest = path.endswith((".py", ".html"))
    is_html_body = first_body.startswith("<")

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
            # HTML opening-signature strictness: when the body's first
            # line starts with `<` and the destination is `.py`/`.html`,
            # require an exact (stripped) first-line match.  Otherwise
            # `<div class="field">` matches `<input type="range">` —
            # both start with `<` — and the wrong window is chosen.
            if is_html_dest and is_html_body:
                if first_window != first_body:
                    continue
            elif first_window_word != first_body_word:
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

    # Snap-first candidate ordering: try the enclosing-element snap
    # BEFORE the raw similarity window.  For an HTML body that
    # reopens a wrapper, the raw window is a mid-element fragment;
    # splicing there produces a nested duplicate instead of a
    # replacement.  The snapped candidate expands to the enclosing
    # element, which is what a "replace" body means.
    candidates: list = []
    snapped = try_ast_snapped_boundaries(base, path, start, end, body_stripped)
    if snapped is not None and snapped != (start, end):
        candidates.append(snapped)
    candidates.append((start, end))
    candidates.extend(try_boundary_pairs(start, end, total))

    saw_noop = False
    for trial_start, trial_end in candidates:
        out = splice_region(
            base, trial_start, trial_end, body_lines, path)
        if out.status == "noop":
            saw_noop = True
            continue
        if out.status == "invalid":
            continue
        return out.content
    if saw_noop:
        return base
    return None


# ============================================================================
#  Decorator-kwarg splice (2026-11-06)
# ============================================================================
#
# See module docstring, DECORATOR KWARG DICT SPLICE (2026-11-06).
#
# The generic target is any `@<decorator>(<kwarg>= {...})` on a
# function or class.  In practice the motivating shapes are all
# `@command(args={...})`, but there is no reason to hardcode `command`
# or `args` at the caller.

_DECORATOR_KWARG_KWARG_DEFAULT = "args"


def _char_offset(text: str, line0: int, col: int) -> Optional[int]:
    """Convert a 0-based `(line, col)` to a character offset in
    `text`, or None if out of range.

    Uses `split("\\n")` and a prefix sum — O(lines).  Callers are
    inside an AST walk over a handful of decorators, so this is
    fine.
    """
    lines = text.split("\n")
    if line0 < 0 or line0 >= len(lines):
        return None
    return sum(len(ln) + 1 for ln in lines[:line0]) + col


def _extract_decorator_kwarg_hint(context_before: str) -> Optional[str]:
    """Try to pull a decorator kwarg name out of prose.

    Recognised shapes:
      "the `args` dict" / "the args dict"
      "the args={...}" / "the `args={...}`"
      "kwargs={...}" / "the kwargs dict"

    Returns None when nothing recognisable is present.  The caller
    treats None as "use the default kwarg name" (`args`).
    """
    if not context_before:
        return None
    m = re.search(
        r'\b([A-Za-z_][\w]*)\s*=\s*\{',
        context_before,
    )
    if m:
        return m.group(1)
    m = re.search(
        r'\bthe\s+`?([A-Za-z_][\w]*)`?\s+'
        r'(?:dict|kwarg|keyword|argument)',
        context_before, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return None


def _find_decorator_kwarg_dicts(base: str, kwarg_name: str) -> list:
    """Return `[(func_name, dict_node, open_char, close_char)]` for
    every decorated function/class whose `@<decorator>(<kwarg>={...})`
    kwarg value is a dict literal.

    Walks the AST, filters by kwarg name, and translates each
    matching `ast.Dict` node to char offsets so callers can splice
    directly.
    """
    try:
        base_tree = parse_source(base)
    except SyntaxError:
        return []

    out = []
    for node in ast.walk(base_tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        for dec in getattr(node, "decorator_list", []) or []:
            if not isinstance(dec, ast.Call):
                continue
            for kw in dec.keywords:
                if kw.arg != kwarg_name:
                    continue
                if not isinstance(kw.value, ast.Dict):
                    continue
                start = _char_offset(
                    base, kw.value.lineno - 1, kw.value.col_offset)
                end = _char_offset(
                    base, kw.value.end_lineno - 1,
                    kw.value.end_col_offset)
                if start is None or end is None:
                    continue
                out.append((node.name, kw.value, start, end))
    return out


def _dict_keys_of(dict_node) -> set:
    """String keys of an `ast.Dict` node."""
    out: set = set()
    for k in dict_node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.add(k.value)
    return out


def _pick_decorator_target(candidates: list, body_keys: set,
                            scope_name: Optional[str]) -> Optional[tuple]:
    """Choose among `_find_decorator_kwarg_dicts` results.

    Selection order:
      1. If `scope_name` matches a candidate's function name, use it.
      2. Otherwise, score each candidate by key overlap with the
         body's keys.  The winner must beat the runner-up by at
         least 3 keys; ties and near-ties fail-closed (return None).
      3. If there is only one candidate, use it.
    """
    if not candidates:
        return None

    if scope_name:
        for c in candidates:
            if c[0] == scope_name:
                return c

    if len(candidates) == 1:
        return candidates[0]

    scored = []
    for c in candidates:
        func_name, dict_node, _start, _end = c
        existing = _dict_keys_of(dict_node)
        overlap = len(body_keys & existing)
        scored.append((overlap, func_name, c))
    scored.sort(key=lambda x: -x[0])

    if not scored:
        return None
    best_overlap = scored[0][0]
    second_overlap = scored[1][0] if len(scored) > 1 else -1

    if best_overlap == 0:
        # No key overlap anywhere — ambiguous.
        return None
    if second_overlap == best_overlap:
        # Tie — fail-closed.
        return None
    if best_overlap - second_overlap < 3:
        # Too close — fail-closed.
        return None
    return scored[0][2]


def _splice_decorator_kwarg(base: str, path: str,
                             kwarg_name: str, dict_text: str,
                             context_before: str) -> Optional[str]:
    """Replace the value of a decorator kwarg dict with `dict_text`.

    Returns new content on success, `base` on no-op (all body keys
    already present), or None when no candidate is unambiguous.

    The `intent_satisfied_in` check is skipped here: the body may
    legitimately change the *values* of existing keys, which
    `intent_satisfied_in` cannot detect (it only checks key presence).
    The already-present fast path lives upstream in
    `_is_already_applied` — if that returned True, we never reach
    this helper.
    """
    try:
        body_tree = parse_source(dict_text)
    except SyntaxError:
        return None
    if (len(body_tree.body) != 1
            or not isinstance(body_tree.body[0], ast.Expr)
            or not isinstance(body_tree.body[0].value, ast.Dict)):
        # Wrap `{...}` in an expression so ast.parse accepts it.
        try:
            body_tree = parse_source("_ = " + dict_text)
        except SyntaxError:
            return None
        if (len(body_tree.body) != 1
                or not isinstance(body_tree.body[0], ast.Assign)
                or not isinstance(body_tree.body[0].value, ast.Dict)):
            return None
        body_keys = _dict_keys_of(body_tree.body[0].value)
    else:
        body_keys = _dict_keys_of(body_tree.body[0].value)

    from .patch_scope_insert import _extract_scope_name_from_prose
    scope_name = _extract_scope_name_from_prose(context_before or "")

    candidates = _find_decorator_kwarg_dicts(base, kwarg_name)
    target = _pick_decorator_target(candidates, body_keys, scope_name)
    if target is None:
        return None

    _func_name, _dict_node, start, end = target
    result = base[:start] + dict_text + base[end:]

    from .patch_bounds import structural_valid
    if not structural_valid(result, path):
        return None
    return result if result != base else base


def _merge_decorator_kwarg_entry(base: str, path: str,
                                  kwarg_name: str, new_key: str,
                                  entry_text: str,
                                  context_before: str) -> Optional[str]:
    """Insert a dict entry into an existing decorator kwarg dict.

    Returns new content on success, `base` on no-op (key already
    present), or None when no candidate is unambiguous.

    Reuses `patch_misc_insert._splice_dict_entry` for the actual
    indentation-aware insertion.
    """
    from .patch_scope_insert import _extract_scope_name_from_prose
    scope_name = _extract_scope_name_from_prose(context_before or "")

    candidates = _find_decorator_kwarg_dicts(base, kwarg_name)
    if not candidates:
        return None

    # Filter out candidates that already contain the key.
    without_key = [c for c in candidates
                   if new_key not in _dict_keys_of(c[1])]
    with_key = [c for c in candidates
                if new_key in _dict_keys_of(c[1])]

    # If the scope-named function's dict already has the key, that is
    # the no-op signal.
    if scope_name:
        for c in with_key:
            if c[0] == scope_name:
                return base
        for c in without_key:
            if c[0] == scope_name:
                return _do_merge_into(base, path, c[2], c[3], entry_text)

    if not without_key:
        # Every candidate already has the key.
        if len(with_key) == len(candidates):
            return base
        return None

    if len(without_key) == 1:
        c = without_key[0]
        return _do_merge_into(base, path, c[2], c[3], entry_text)

    # Multiple candidates without the key.  Use key overlap on the
    # EXISTING keys (the dict we're merging into).  Not great but
    # bounded by fail-closed.
    scored = []
    for c in without_key:
        existing = _dict_keys_of(c[1])
        # We don't know the body's full key set for a merge, so
        # use a heuristic: candidate's key count as a proxy for
        # "which decorator is this".  In practice the smallest dict
        # without the key tends to be the intended target for a
        # single-entry insert.  See patch_misc_insert._try_named_dict_insert
        # for the same fallback pattern.
        scored.append((len(existing), c))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0])

    if len(scored) >= 2 and scored[0][0] == scored[1][0]:
        # Tie on size — fail-closed.
        return None

    c = scored[0][1]
    return _do_merge_into(base, path, c[2], c[3], entry_text)


def _do_merge_into(base: str, path: str,
                    open_char: int, close_char: int,
                    entry_text: str) -> Optional[str]:
    """Splice `entry_text` before the closing `}` of the dict whose
    `{` is at `open_char` and `}` is at `close_char`.
    """
    from .patch_misc_insert import _splice_dict_entry
    from .patch_bounds import structural_valid
    result = _splice_dict_entry(base, entry_text, open_char, close_char)
    if result is None:
        return None
    if not structural_valid(result, path):
        return None
    return result


def _try_decorator_kwarg_splice(base: str, body: str, path: str,
                                 context_before: str = "") -> Optional[str]:
    """Structural decorator-kwarg dict splice.

    See module docstring, DECORATOR KWARG DICT SPLICE (2026-11-06).

    Body shapes recognised:
      1. `name = {...}` (possibly with trailing content) — replace
         the corresponding decorator kwarg dict `name`.
      2. Bare dict entry `"key": val,` — merge into a decorator
         kwarg dict that lacks this key.

    The target is identified via `_pick_decorator_target`: the
    scope named in `context_before` if any, else the candidate with
    the largest unique key overlap.

    Returns new content on success, `base` on no-op, or None.
    """
    if not path.endswith(".py"):
        return None
    body_stripped = body.strip()
    if not body_stripped:
        return None

    # ---- Case 1: `name = {...}` ----
    m = re.search(
        r'^\s*([A-Za-z_][\w]*)\s*=\s*\{',
        body_stripped, re.MULTILINE)
    if m:
        kwarg_name = m.group(1)
        brace_start = body_stripped.find("{", m.start())
        if brace_start >= 0:
            brace_end = _find_matching_brace(
                body_stripped, brace_start, is_python=True)
            if brace_end > brace_start:
                dict_text = body_stripped[brace_start:brace_end + 1]
                return _splice_decorator_kwarg(
                    base, path, kwarg_name, dict_text, context_before)

    # ---- Case 2: bare entry `"key": val,` ----
    try:
        wrapped = parse_source(
            "_ = {" + body_stripped.rstrip(",") + "}")
    except SyntaxError:
        return None
    if not (len(wrapped.body) == 1
            and isinstance(wrapped.body[0], ast.Assign)
            and isinstance(wrapped.body[0].value, ast.Dict)):
        return None
    dict_node = wrapped.body[0].value
    if len(dict_node.keys) != 1:
        return None
    k = dict_node.keys[0]
    if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
        return None
    new_key = k.value

    entry_text = body_stripped.rstrip()
    if not entry_text.endswith(","):
        entry_text += ","

    # Determine the target kwarg name: prose hint or default `args`.
    kwarg_name = (_extract_decorator_kwarg_hint(context_before)
                  or _DECORATOR_KWARG_KWARG_DEFAULT)
    return _merge_decorator_kwarg_entry(
        base, path, kwarg_name, new_key, entry_text, context_before)


# ============================================================================
#  Tool-bug detection (2026-10-29)
# ============================================================================
#
# `_try_deterministic_patch` is the entry point for the whole heuristic
# chain.  Its historical `except Exception` caught every raise and
# returned None, meaning "the heuristics declined this body."  That is
# correct for the common case: an `AttributeError` in a heuristic that
# mis-parsed a weird body shape, a `ValueError` from text manipulation,
# a `SyntaxError` from user-supplied content.
#
# It is WRONG for the case where the heuristic module itself is broken
# — a stale import, a renamed function referenced by name, a module
# that landed in the wrong directory.  Those are bugs in deeppeek, not
# failures of the body, and they abort the entire remaining chain.
#
# `_TOOL_BUG_EXCEPTIONS` marks the types that are code invariants
# rather than data conditions.  When the wrapper catches one, it
# records `patch["tool_bug"] = "..."` on the patch dict and returns
# None (same fail-closed behavior).  Downstream passes now see a
# distinguishable signal: `pass6_apply_patches` uses the recorded
# exception as `failure_reason` (skipping `_diagnose_patch_failure`,
# which would produce an unhelpful "no heuristic matched" message),
# adds a "deeppeek bug" warning to `rec.warnings`, and sets
# `rec.verification["tool_bug"]`; `pass9_write` surfaces a
# `[TOOL_BUG]` line in `warnings.txt`.
_TOOL_BUG_EXCEPTIONS = (ImportError, ModuleNotFoundError, NameError)


def _try_deterministic_patch(base, patch, path="",
                              prior_insertions=None, prior_scopes=None):
    try:
        return _try_deterministic_patch_inner(
            base, patch, path,
            prior_insertions=prior_insertions,
            prior_scopes=prior_scopes,
        )
    except _TOOL_BUG_EXCEPTIONS as e:
        import sys
        patch["tool_bug"] = f"{type(e).__name__}: {e}"
        print(f"  [tool-bug] deterministic attempt raised "
              f"{type(e).__name__}: {e} — this is a deeppeek bug, "
              f"not a body-shape failure", file=sys.stderr)
        return None
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
    scope_hint = patch.get("scope_hint", "") or ""

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

    single_line = _try_single_line_replace(
        base, raw_body, context_before, path=path)
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
            base, raw_body, context_before, path,
            scope_hint=scope_hint,
        )
        if _decorated_scope_result is not None:
            return _decorated_scope_result

    # NEW (2026-11-06): decorator kwarg splice.  Runs before the
    # object-literal branch so `args={...}` bodies targeting a
    # decorator kwarg are handled structurally, not routed through
    # `_try_object_literal_splice` (which refuses trailing content
    # and picks the wrong decorator when multiple `args={...}` exist).
    _decor_result = _try_decorator_kwarg_splice(
        base, body_stripped, path, context_before)
    if _decor_result is not None:
        return _decor_result

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
        base, raw_body, context_before, path,
        scope_hint=scope_hint,
    )
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
