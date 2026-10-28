"""
patch_predicates — pure predicates shared by the insert heuristics.

No imports beyond `re`; no imports from other project modules.  This
is the leaf module of the patch-heuristics subtree.  Every function
here is a pure text predicate — no mutation, no side effects, no
dependency on the state of a `Block` or `FileRecord`.

STABLE: these predicates encode "what counts as a complete body",
"what counts as a safe anchor", and "what counts as a code signal".
They change only when the *definition* of those concepts changes,
which is rare.  Adding a new heuristic means calling these, not
editing them.

Why a separate module:

  · The predicates are shared across `patch_scope_insert`,
    `patch_anchor_insert`, and `patch_misc_insert` — three modules
    with different churn profiles.  Keeping the predicates in one
    place means a change (e.g. accepting one more HTML tag as
    "complete") propagates to every consumer with one edit.

  · They are testable in isolation: no fixtures, no base content,
    just a string and a boolean.  The unit-test surface is tiny.

  · They are the most stable part of the module.  Isolating them
    means a future refactor of the insertion logic cannot
    accidentally touch the definition of "complete statement".
"""

import re


# ===========================================================================
#  Continuation markers  (2026-10-28e)
# ===========================================================================
#
# Suffixes that mark a line as mid-expression rather than a completed
# statement: a trailing binary/assignment operator, a line
# continuation, a bare scope opener (`:`), an unclosed paren/bracket
# on the same line, or the Python boolean operators `and` / `or`.
#
# `\b` on `and` / `or` prevents false positives on identifiers that
# happen to end with those letters (`band`, `floor`, `understand`).
#
# Kept as a compiled regex rather than a suffix tuple because
# `str.endswith("and")` would also match `"band"`, `"sand"`, and
# other identifiers where `and` is only a substring.

_CONTINUATION_END_RE = re.compile(
    r'(?:[,+*/\\=|&(]|\band\b|\bor\b|:)$'
)


def _has_unclosed_brackets(text):
    """Return True iff `text` has unclosed `(`, `[`, or `{` brackets.

    Brackets inside string literals (single-, double-quoted, or
    backticked) are ignored.  Escape sequences (`\\"`, `\\'`, ``\\```)
    inside strings are honored.

    Used by `_body_is_complete_at_boundary` as the "is this a complete
    expression?" half of the language-agnostic check: a body ending in
    a bare assignment (`x = 1`) is balanced and complete, while a body
    opening a bracketed group without closing it (`x = (`) is not.
    """
    if not text:
        return False
    depth = 0
    i = 0
    n = len(text)
    in_str = None
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'", chr(96)):
            in_str = ch
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return depth != 0


def _body_is_complete_at_boundary(body):
    """True iff `body` can be inserted between two lines as a
    complete statement.

    Complete = last non-blank line ends with `;`, `}`, `)`, or is a
    closing HTML tag (`</details>`, `</summary>`, …), OR the body is a
    single-line comment (`// ...` or `# ...`), OR the body is a
    standalone block comment (`/* ... */`), OR the body is
    bracket-balanced (no unclosed `(`, `[`, `{`) and the last
    non-blank line does not end with a continuation marker (`,`, `\\`,
    a binary operator, `and`, `or`, or a bare `:`).

    The last clause makes the check language-agnostic.  Python
    statements like `x = 1` and `return x` — which have no trailing
    `;` — are recognized as complete; so are multi-line constructs
    ending in an assignment (`try: ... output = 0`).  JS statements
    using ASI (`x = 1` without `;`) are accepted the same way.

    Bodies that open a bracketed group without closing it (`x = (`,
    `[1, 2`), that end mid-expression (`x = 1 +`), or that end in a
    scope opener (`def foo():`, `if x:`) are still rejected.

    The check fires at the top of every insert heuristic
    (`_try_insert_before_anchor`, `_try_insert_between_anchors`,
    `_try_insert_after_anchor`) because those insert a body
    *adjacent to* existing lines; an incomplete body would corrupt
    the surrounding code.  The check is deliberately strict about
    *structure* (unclosed brackets, dangling operators) and
    deliberately lenient about *statement terminators* (no `;`
    requirement), so a well-formed body from any language is
    accepted.
    """
    if not body or not body.strip():
        return False
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1].strip()
    if last.startswith("//") or last.startswith("#"):
        return True
    if last.startswith("/*") and last.endswith("*/"):
        return True
    if last.endswith((";", "}", ")")):
        return True
    if last == "}":
        return True
    if last.startswith("</"):
        return True
    if not _has_unclosed_brackets(body):
        if not _CONTINUATION_END_RE.search(last):
            return True
    return False


def _context_insert_ok(anchor_line, body):
    """The body can be inserted adjacent to the anchor iff either:

    - the body is a complete statement (last non-blank line ends with
      `;`, `}`, `)`, `</...>`, or is a comment), OR
    - the anchor line ends with `,`, so the body is being appended
      to a comma-separated container (object literal, array,
      parameter list).

    The comma-anchor case covers the object-entry insert pattern:
    `key: value,` before `other: value,` is a valid addition to the
    object literal even though the added line itself ends with `,`.
    Without this branch, `inpaintFullCanvas: false,` before
    `inpaintPrompt: "",` would be refused because neither line is
    a "complete statement" by the strict predicate.
    """
    if _body_is_complete_at_boundary(body):
        return True
    if anchor_line.strip().endswith(","):
        return True
    return False


def _anchor_line_is_safe(base, line_start):
    """True iff inserting AFTER the line at `line_start` is safe.

    A line is unsafe iff it opens more brackets than it closes:
    inserting after it lands inside the enclosing block.
    """
    line_end = base.find("\n", line_start)
    if line_end == -1:
        line_end = len(base)
    line = base[line_start:line_end]
    opens = (line.count("{") + line.count("(") + line.count("["))
    closes = (line.count("}") + line.count(")") + line.count("]"))
    return opens <= closes


_CLOSER_ONLY_RE = re.compile(r'^[}\]\)]+[;,)]*$')

_STRONG_CODE_SIGNAL_RE = re.compile(r'[=({\[;]')


def _line_is_closer_only(line):
    return bool(_CLOSER_ONLY_RE.match(line.strip()))


def _line_has_code_signal(line):
    """True iff `line` contains at least one character that is a
    near-certain code marker: `=`, `(`, `{`, `[`, `;`.  Prose and
    docstring mentions do not have these."""
    return bool(_STRONG_CODE_SIGNAL_RE.search(line))
