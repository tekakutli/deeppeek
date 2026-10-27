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
