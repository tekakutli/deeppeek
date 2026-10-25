"""Structural brace-truncation repair SUGGESTIONS.

When a conversation block applies but leaves the file with more
unclosed `{` than closers, `validate_content` correctly rejects the
record and preserves the target.  Some of those rejections are
*structural* — the block's intent is unambiguous from its own shape —
and a small, deterministic repair can produce a valid candidate for
the user to review.

This module generates those suggestions.  It does NOT modify the
target tree.  The `.repaired` file lands in `low_trust/` beside the
original; the user inspects the diff and copies it over manually if
they accept it.  That boundary is deliberate — the invariant
"deeppeek never corrupts the target tree" must hold even for
suggested repairs.

High-confidence criteria — all must hold:

  1. The structural failure is EXACTLY `unbalanced braces (+N)`,
     N >= 1.  Parens/brackets/string-literal imbalance means
     mid-expression or quote truncation, which no deterministic
     rule can repair.

  2. The block body's brace depth never goes negative at any
     prefix.  A premature `}` (block closes something it didn't
     open) is a different failure mode, not a truncation.

  3. The block body's last non-blank line ends with `;`, `}`, or
     `)`.  That is a complete statement; anything else means the
     block ends mid-expression and repair would produce only
     syntactically-balanced nonsense.

  4. After the INNERMOST unclosed `{`, no non-blank line is either
     a non-close at that opener's indent or shallower.  The
     innermost criterion is what makes this "high confidence":
     a non-close line at the innermost opener's indent means a
     sibling statement exists that would be silently nested
     inside the repaired block.  That is the pg_panel_fields.py
     shape — semantically wrong, not just structurally off.
     Refuse.

  An earlier version of this module used the OUTERMOST unclosed
  opener for criterion 4, which passed both the pg_panel_fields.py
  semantic case and the pg_panel_scrubs.py wiring case.  The
  innermost criterion catches both, at the cost of refusing a few
  genuinely structural truncations where a sibling statement
  happened to be at the innermost opener's indent.  That is the
  correct trade-off: a false refusal costs the user one manual
  edit; a false acceptance costs them a silent semantic change.

Fail-closed rules for `try_repair`:

  · Any criterion fails → return None
  · The repaired body can't be re-inserted into the base via
    `_try_deterministic_patch` → return None
  · `validate_content` on the repaired full file fails → return None

No exceptions propagate to the caller; every failure path prints a
short `[repair]` diagnostic to stderr and returns None so the record
stays fail-closed.
"""

import re
from typing import Optional

from .core import FileRecord
from .validate_output import _js_balance, validate_content


_BRACE_FAILURE_RE = re.compile(r"unbalanced braces \(\+(\d+)\)")
_STATEMENT_TERMINATORS = (";", "}", ")")


# ---------------------------------------------------------------------------
# Character-level brace analysis (strings/comments aware)
# ---------------------------------------------------------------------------

def _scan_braces(text: str):
    """Yield (char_index, char, depth_after) for every `{`/`}` outside
    strings and comments."""
    depth = 0
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'", chr(96)):
            in_str = c
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                j = text.find("\n", i + 2)
                i = n if j == -1 else j
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        if c == "{":
            depth += 1
            yield i, c, depth
        elif c == "}":
            depth -= 1
            yield i, c, depth
        i += 1


def _balance_never_negative(text: str) -> bool:
    for _i, _c, depth in _scan_braces(text):
        if depth < 0:
            return False
    return True


def _unclosed_openers(text: str) -> list:
    """Return [(line_index, indent)] for every unclosed `{`, in push
    order.  Empty list when the text is balanced."""
    stack = []
    for char_index, c, _depth_after in _scan_braces(text):
        line_index = text.count("\n", 0, char_index)
        lines = text.split("\n")
        line = lines[line_index] if line_index < len(lines) else ""
        indent = len(line) - len(line.lstrip())
        if c == "{":
            stack.append((line_index, indent))
        elif c == "}":
            if stack:
                stack.pop()
    return stack


# ---------------------------------------------------------------------------
# Safety criteria
# ---------------------------------------------------------------------------

def _last_line_is_complete(text: str) -> bool:
    for line in reversed(text.split("\n")):
        stripped = line.rstrip()
        if not stripped:
            continue
        return stripped.endswith(_STATEMENT_TERMINATORS)
    return False


def _truncation_is_purely_structural(text: str,
                                      openers: list) -> bool:
    """Innermost unclosed opener's inner content is entirely
    deeper-indented.  Any non-close at or shallower than the
    innermost opener's indent is a sibling that would be silently
    nested by repair — refuse."""
    if not openers:
        return False
    innermost_line, innermost_indent = openers[-1]
    lines = text.split("\n")
    for idx in range(innermost_line + 1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("}", "};"):
            continue  # a close is fine; exits an ancestor
        indent = len(line) - len(line.lstrip())
        if indent <= innermost_indent:
            return False  # sibling statement — semantic, not structural
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def try_repair(rec: FileRecord,
               structural_err: str,
               blocks_by_idx: dict,
               path: str,
               ) -> Optional[dict]:
    """Return a repair suggestion dict, or None.  Never raises."""
    m = _BRACE_FAILURE_RE.search(structural_err or "")
    if not m:
        return None  # not a brace failure — no repair applies
    n_closers = int(m.group(1))
    if n_closers <= 0:
        return None

    if blocks_by_idx is None or rec.last_full_idx is None:
        return {"refused_reason": "no base block available"}
    base_block = blocks_by_idx.get(rec.last_full_idx)
    if base_block is None:
        return {"refused_reason": "base block not found"}
    base = base_block.body

    for patch in rec.patches:
        body = patch.get("body", "")
        if not body.strip():
            continue
        result = _try_repair_one(patch, body, base, n_closers, path)
        if result is not None:
            return result
    return {"refused_reason": "no patch body matched the brace failure"}


def _try_repair_one(patch, body: str, base: str,
                    n_closers: int, path: str) -> Optional[dict]:
    b, p_, k, u = _js_balance(body)
    if b != n_closers or p_ != 0 or k != 0 or u:
        return {"refused_reason": (
            f"patch body does not match failure "
            f"(brace delta {b}, expected {n_closers}; "
            f"parens {p_}, brackets {k})")}

    if not _balance_never_negative(body):
        return {"refused_reason": "block has a premature `}`"}

    if not _last_line_is_complete(body):
        return {"refused_reason": "block's last line ends mid-expression"}

    openers = _unclosed_openers(body)
    if len(openers) != n_closers:
        return {"refused_reason": (
            f"opener stack count {len(openers)} != unbalance {n_closers}")}

    if not _truncation_is_purely_structural(body, openers):
        return {"refused_reason": "sibling statement at "
                "innermost opener's indent (semantic truncation)"}

    closer_lines = [(" " * indent) + "}" for _ln, indent in reversed(openers)]
    repaired_body = body.rstrip() + "\n" + "\n".join(closer_lines) + "\n"

    b2, p2, k2, u2 = _js_balance(repaired_body)
    if (b2, p2, k2, u2) != (0, 0, 0, 0):
        return {"refused_reason": (
            f"repaired body still unbalanced: "
            f"{b2},{p2},{k2},{u2}")}

    from .apply_patches import _try_deterministic_patch
    patch_dict = {
        "body": repaired_body,
        "context_before": patch.get("context_before", ""),
    }
    try:
        repaired_content = _try_deterministic_patch(
            base, patch_dict, path=path)
    except Exception as e:
        return {"refused_reason": f"re-insertion raised: {e}"}

    if repaired_content is None:
        return {"refused_reason": "repaired block could not be "
                "re-inserted into base"}

    try:
        err = validate_content(repaired_content, path, base=base)
    except Exception as e:
        return {"refused_reason": f"validation raised: {e}"}
    if err is not None:
        return {"refused_reason": f"repaired content fails "
                f"validation: {err}"}

    return {
        "repaired_body": repaired_body,
        "repaired_content": repaired_content,
        "closers_added": n_closers,
        "refused_reason": None,
    }
