"""Pure text helpers for pass 9.

VERY STABLE.  Every function here is a pure string-or-record-in,
string-out helper with no external state.  They change only when the
format of a manifest field or a warnings line changes, which is rare.
When a new "New in 2026-XX" annotation adds a per-patch reason string,
that string is usually threaded through `_patch_first_line` /
`_patch_context` rather than changing them.

Keep this file small.  If a helper starts needing `records`, `blocks`,
or filesystem state, it belongs in `_disposition.py` or `pass9.py`.
"""

import re

from ..core import Block, FileRecord


# ============================================================================
#  Per-block / per-patch first-line and context previews
# ============================================================================

def _orphan_first_line(block: Block) -> str:
    """First non-empty body line of an orphan block, truncated."""
    if not block.body:
        return "(empty)"
    for ln in block.body.splitlines():
        s = ln.strip()
        if s:
            if len(s) > 60:
                s = s[:57] + "..."
            return s
    return "(empty)"


def _patch_first_line(patch: dict, limit: int = 60) -> str:
    """First non-empty line of a patch's body, truncated."""
    body = patch.get("body", "") or ""
    for ln in body.splitlines():
        s = ln.strip()
        if s:
            return s[:limit]
    return ""


def _patch_context(patch: dict, limit: int = 800) -> str:
    """Return the patch's `context_before`, truncated to `limit`
    characters with a trailing ellipsis marker when longer.

    FIX (2026-11-01, d): when a heuristic refuses a patch because
    the surrounding prose didn't match any of its patterns, the
    manifest needs to show the prose the heuristic saw.  Without
    it, the diagnostic string ("no heuristic matched…") is an
    opaque verdict that can't be acted on without re-reading the
    dump.  `context_before` closes that gap.
    """
    ctx = (patch.get("context_before") or "").strip()
    if len(ctx) <= limit:
        return ctx
    return ctx[:limit - 3] + "..."


# ============================================================================
#  Trust reason (used by warnings.txt and manifest decisions)
# ============================================================================

def _trust_reason(rec: FileRecord) -> str:
    """Human-readable string listing which check(s) triggered LOW trust.

    FIX (2026-11-01, b): names the specific check rather than emitting
    an opaque `trust=LOW`.  That is what the operator reads in
    warnings.txt when a file lands in `low_trust/`.
    """
    reasons = []
    if rec.verification.get("patches_incomplete"):
        reasons.append("patches_incomplete")
    if rec.verification.get("pairs_incomplete"):
        reasons.append("pairs_incomplete")
    ast_check = rec.verification.get("ast_parse", "")
    if isinstance(ast_check, str) and ast_check.startswith("FAIL"):
        reasons.append("ast_parse_failed")
    if reasons:
        return " [" + ", ".join(reasons) + "]"
    return ""


# ============================================================================
#  Warning aggregation  (2026-11-05)
# ============================================================================
#
# `warnings.txt` used to emit one line per record per warning class:
# 200 untouched records with the same structural-equivalence warning
# produced 200 lines, all identical apart from the path.  The two
# helpers below collapse identical-class warnings into:
#
#     [<first_path>] [WARN] <text> (+199 more file(s) with this warning)
#
# One readable line, full per-record detail still in
# `manifest.decisions[path].warnings`.

_WARN_CLASS_VAR_RE = re.compile(
    r'`[^`]+`'
    r'|\bline \d+\b'
    r'|\b\d+\b'
    r'|\[[^\]\n]+\]'
)


def _warn_class_key(text: str) -> str:
    """Stable class key for a warning text.

    Strips the variable parts — backticked names, line numbers, bare
    numbers, bracketed paths — and truncates to 60 characters.  Two
    warnings with different variable fragments but the same shape
    produce the same key, so they aggregate; two warnings with
    genuinely different text produce different keys and stay
    separate.
    """
    return _WARN_CLASS_VAR_RE.sub("_", text)[:60]


def _aggregate_warnings(warnings_lines: list) -> list:
    """Collapse runs of identical-class warnings into one line + count.

    Preserves the per-record `[path] [WARN] <text>` lines for warnings
    whose text differs, but a warning class that fires on N records
    becomes:

        [<path>] [WARN] <text> (+{N-1} more file(s) with this warning)

    Non-`[WARN]` lines are preserved verbatim in their original order
    — this function only touches the `[<path>] [WARN] <text>` shape.

    The function iterates its input exactly **once**, accumulating
    grouped entries into `warn_groups` and pushing every line —
    aggregated or verbatim — into the single output list.  A prior
    version appended non-warning lines during classification *and*
    during reconstruction, doubling every non-`[WARN]` line in the
    output; the 2026-11-05 test run reported one duplicate
    `[preexisting]` line per untouched summary block.
    """
    warn_pattern = re.compile(r'^\[([^\]]+)\] \[WARN\] (.+)$')
    warn_groups: dict[str, list] = {}
    order: list[tuple[str, str]] = []

    for line in warnings_lines:
        m = warn_pattern.match(line)
        if not m:
            order.append(("other", line))
            continue
        path, text = m.group(1), m.group(2)
        key = _warn_class_key(text)
        if key not in warn_groups:
            warn_groups[key] = []
            order.append(("warn", key))
        warn_groups[key].append((path, text))

    result: list = []
    for kind, val in order:
        if kind == "other":
            result.append(val)
            continue
        entries = warn_groups[val]
        if len(entries) == 1:
            path, text = entries[0]
            result.append(f"[{path}] [WARN] {text}")
        else:
            sample_path, sample_text = entries[0]
            result.append(
                f"[{sample_path}] [WARN] {sample_text} "
                f"(+{len(entries) - 1} more file(s) with this warning)"
            )
    return result
