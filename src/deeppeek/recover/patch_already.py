"""
patch_already — deletion, diff, and already-present predicates.

Five helpers that either mutate the base (deletion, raw diff) or
check "has this patch's intent already landed?" before failing:

  _delete_exact_block         remove a uniquely-matched block
  _deletion_already_applied   check whether the block is already gone
  _try_raw_diff_patch         apply a raw unified diff body
  _diff_already_applied       check whether the diff's additions are
                              already present
  _patch_already_present      broader check (exact or token-equivalent
                              subset) that a patch was already applied

Additionally:

  _exact_lines_present        sliding-window exact match (short-body
                              pre-check; stricter than
                              `_patch_already_present`)
  _max_contiguous_run         longest contiguous run of body lines
                              matching a base window (guard against
                              token-dispersed bodies being mistaken
                              for already-applied)

VERY STABLE: these encode well-understood primitives ("did the edit
land?").  New conversations rarely force changes here — they surface
as new *shapes* to feed into these checks, not as new checks
themselves.
"""

from .core import (
    _is_pure_deletion_diff_strict,
    _looks_like_diff_body,
    _strip_diff_prefixes,
)
from .patch_text import _content_present_in_base


def _delete_exact_block(base, body):
    is_diff = _is_pure_deletion_diff_strict(body) or _looks_like_diff_body(body)
    stripped = _strip_diff_prefixes(body) if is_diff else body
    body_lines = [ln.rstrip() for ln in stripped.splitlines()]
    while body_lines and not body_lines[0]:
        body_lines.pop(0)
    while body_lines and not body_lines[-1]:
        body_lines.pop()
    if not body_lines:
        return None
    base_lines_keepends = base.splitlines(keepends=True)
    base_lines = [ln.rstrip("\n").rstrip() for ln in base_lines_keepends]
    n = len(body_lines)
    matches = []
    for i in range(len(base_lines) - n + 1):
        if base_lines[i:i+n] == body_lines:
            matches.append(i)
    if len(matches) != 1:
        return None
    start = matches[0]
    end = start + n
    new_lines = base_lines_keepends[:start] + base_lines_keepends[end:]
    new_text = "".join(new_lines)
    if new_text == base:
        return None
    return new_text


def _deletion_already_applied(body, base):
    stripped = _strip_diff_prefixes(body)
    body_lines = [ln.rstrip() for ln in stripped.splitlines() if ln.strip()]
    if not body_lines:
        return False
    base_nonempty = [ln.rstrip() for ln in base.splitlines() if ln.strip()]
    n = len(body_lines)
    for i in range(len(base_nonempty) - n + 1):
        if base_nonempty[i:i+n] == body_lines:
            return False
    return True


def _try_raw_diff_patch(base, body):
    diff_lines = body.splitlines()
    if not any(ln.startswith(("+", "-")) for ln in diff_lines):
        return None
    if any(ln.startswith(("+++", "---", "@@")) for ln in diff_lines):
        return None
    first_anchor = None
    for ln in diff_lines:
        if not ln:
            continue
        prefix = ln[:1]
        if prefix in (" ", "-"):
            first_anchor = ln[1:].rstrip()
            break
    if first_anchor is None:
        return None
    base_lines = base.splitlines(keepends=True)
    anchors = [
        idx for idx, ln in enumerate(base_lines)
        if ln.rstrip() == first_anchor
    ]
    if not anchors:
        return None
    for start in anchors:
        result = base_lines[:start]
        pos = start
        i = 0
        ok = True
        while i < len(diff_lines):
            line = diff_lines[i]
            i += 1
            if not line:
                line = " "
            prefix = line[:1]
            rest = line[1:]
            if prefix == " ":
                if pos >= len(base_lines) or (
                        base_lines[pos].rstrip() != rest.rstrip()):
                    ok = False
                    break
                result.append(base_lines[pos])
                pos += 1
            elif prefix == "-":
                if pos >= len(base_lines) or (
                        base_lines[pos].rstrip() != rest.rstrip()):
                    ok = False
                    break
                pos += 1
            elif prefix == "+":
                result.append(rest + "\n")
        if ok:
            result.extend(base_lines[pos:])
            new_text = "".join(result)
            if new_text == base:
                return None
            return new_text
    return None


def _diff_already_applied(body, base):
    if not _looks_like_diff_body(body):
        return False
    adds = []
    dels = []
    for ln in body.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            adds.append(ln[1:].rstrip())
        elif ln.startswith("-") and not ln.startswith("---"):
            dels.append(ln[1:].rstrip())
    base_lines_set = {ln.rstrip() for ln in base.splitlines()}
    for a in adds:
        if a and a not in base_lines_set:
            return False
    dels_nonempty = [d for d in dels if d.strip()]
    if dels_nonempty:
        base_lines_nonempty = [
            ln.rstrip() for ln in base.splitlines() if ln.strip()]
        n = len(dels_nonempty)
        for i in range(len(base_lines_nonempty) - n + 1):
            if base_lines_nonempty[i:i + n] == dels_nonempty:
                return False
    return True


def _patch_already_present(body, base):
    """True iff the patch's content is present in base.

    Exact-match fast path: a sliding window over base matches the
    body's line list exactly.

    DRIFT-FIX: after the exact check fails, run a token-equivalent
    membership check via `_content_present_in_base`.  The base may
    render the post-state slightly differently from how the patch
    wrote it (reordered conjuncts, reformatted expressions).  If 85%
    of the patch's lines are token-equivalent-present in base, the
    patch's intent is already reflected and the caller should mark
    this patch `already-present` rather than `failed`.

    NOTE: the token-equivalent fallback is a *dispersed* check — it
    is satisfied if the body's tokens appear ANYWHERE in base, not
    necessarily contiguously.  Callers that need to distinguish
    "content is genuinely already applied" from "these tokens happen
    to exist in base" should gate on `_max_contiguous_run` (see
    `patch_dispatch._try_deterministic_patch_inner`'s already-present
    pre-check).
    """
    def norm(s):
        return [ln.strip() for ln in s.splitlines() if ln.strip()]
    body_lines = norm(body)
    if not body_lines:
        return True
    base_lines = norm(base)
    n = len(body_lines)
    if n <= len(base_lines):
        for i in range(len(base_lines) - n + 1):
            if base_lines[i:i+n] == body_lines:
                return True
    if _content_present_in_base(base, body, threshold=0.85):
        return True
    return False


# ===========================================================================
#  Exact contiguous pre-check (2026-10-28f)
# ===========================================================================
#
# Motivating cases: re-running deeppeek on an already-modified tree.
# The pipeline's outer loop checks `_patch_already_present` only
# AFTER `_try_deterministic_patch` returns — but if a heuristic fires
# (scoped-replace, anchor-after, structural-append, ...), the outer
# check is skipped and the modified content is applied.  For a
# reprint of a block whose content is already in base (from the
# previous run), this produced a duplicated paragraph, a duplicated
# try/except block, whitespace drift in a `<details>` block, and an
# import line inserted mid-file.
#
# `_try_deterministic_patch_inner` now runs an already-present check
# BEFORE any heuristic fires.  The check has two tiers:
#
#   · Short bodies (< 3 lines): `_exact_lines_present` (sliding-
#     window only).  Token fallback would false-positive on a
#     single-line import change (old and new share 85%+ tokens).
#
#   · Multi-line bodies (>= 3 lines): require a contiguous run of
#     >= 95% of body lines matching a base window, AND >= 85% token
#     presence (`_patch_already_present`).  The contiguity gate
#     prevents false positives on bodies whose tokens are dispersed
#     in base (the comfyui.py idx=97 shape: a try/except that
#     introduces new `output_rotation` tokens — mostly present via
#     the base's `output_rotation` string literals — but whose
#     *structure* is not in base).

def _exact_lines_present(body, base):
    """True iff body's stripped lines appear as a contiguous window
    in base's stripped lines.

    Stricter than `_patch_already_present` (which also accepts token-
    equivalent matches at 85% threshold).  Used for the short-body
    already-present pre-check, where token-equivalence is too
    permissive: `import { board } from "./x.js"` and its intended
    replacement `import { board, view } from "./x.js"` share most
    tokens but are distinct patches.
    """
    def norm(s):
        return [ln.strip() for ln in s.splitlines() if ln.strip()]
    body_lines = norm(body)
    if not body_lines:
        return True
    base_lines = norm(base)
    n = len(body_lines)
    if n > len(base_lines):
        return False
    for i in range(len(base_lines) - n + 1):
        if base_lines[i:i+n] == body_lines:
            return True
    return False


def _max_contiguous_run(body, base):
    """Return (longest_run, total_body_lines) where `longest_run` is
    the length of the longest run of consecutive stripped body lines
    that appears as a contiguous window in base.

    A ratio `longest_run / total_body_lines` close to 1.0 means the
    body is substantially already present as a contiguous block.
    The ratio being low despite high token presence (see
    `_patch_already_present`) means the body's tokens are dispersed
    — it is a genuinely new block that happens to share vocabulary
    with base.
    """
    def norm(s):
        return [ln.strip() for ln in s.splitlines() if ln.strip()]
    body_lines = norm(body)
    base_lines = norm(base)
    n = len(body_lines)
    m = len(base_lines)
    if n == 0 or m == 0:
        return 0, n

    base_index: dict = {}
    for i, ln in enumerate(base_lines):
        base_index.setdefault(ln, []).append(i)

    best = 0
    for bi in range(n):
        ln = body_lines[bi]
        for bj in base_index.get(ln, []):
            run = 1
            while (bi + run < n and bj + run < m
                    and body_lines[bi + run] == base_lines[bj + run]):
                run += 1
            if run > best:
                best = run
                if best >= n * 0.95:
                    return best, n
    return best, n
