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


def _body_is_complete_at_boundary(body):
    """True iff `body` can be inserted between two lines as a
    complete statement.

    Complete = last non-blank line ends with `;`, `}`, `)`, or is a
    closing HTML tag (`</details>`, `</summary>`, …),
    OR the body is a single-line comment (`// ...` or `# ...`),
    OR the body is a standalone block comment (`/* ... */`).
    Everything else is either a continuation (ends with `,`,
    `+`, `*`, etc.), an opener (ends with `{` or `(`), or a
    fragment.
    """
    if not body or not body.strip():
        return False
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    first = lines[0].strip()
    last = lines[-1].strip()
    if last.startswith("//") or last.startswith("#"):
        return True
    if last.startswith("/*") and last.endswith("*/"):
        return True
    if last.endswith((";", "}", ")")):
        return True
    if last == "}":
        return True
    # FIX 3: HTML closing tags count as complete.  A block ending
    # with `</details>` closes the tag it opened.  The between-
    # anchors heuristic needs this (the pg_base.py `View` block
    # ends in `</details>`); the before-anchor heuristic inherits
    # it (any complete body is a valid before-anchor insert).
    if last.startswith("</"):
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
