"""pass6_gates — insertion filters and dedup gates for pass 6.

STABLE.  These gates run *before* a candidate result is committed,
or *between* patch applications, to catch shapes the heuristics
alone would accept but that would silently corrupt the target:

  · `_filter_body_by_blocks` — strip body lines that duplicate lines
    already inserted by a previous patch in the same run.

  · `_verify_no_duplicate_insertion` — reject a candidate whose net
    effect duplicates a pre-existing line.

  · `_is_duplicate_assignment_insert` — pre-gate for single-line
    assignments: if the exact line is already in base, mark the
    patch `already-present` before running the (expensive)
    heuristic chain.

  · `_is_blockquoted_strong` / `_try_blockquoted_patch` — the
    blockquote policy: blockquoted content is thought-fragment
    unless attribution is strong (exact-prose with commit language)
    AND the candidate result passes structural validation.

  · `_record_single_line_anchor` / `_mark_already_present` —
    bookkeeping for the prior-insertions fallback: record the line
    a patch inserted, so a subsequent patch whose prose says
    "add after:" can anchor on it.

Extracted from the pre-2026-11-05 `apply_patches.py`.  The main loop
and the structural/sanity gates live in `apply_patches.py` and
`pass6_sanity.py` respectively.

ORDERING
========
`_is_duplicate_assignment_insert` runs at the very top of the patch
processing loop, before `_try_replace_intent`.  Its purpose is to
catch the shape where a single-line assignment is already in base
and every heuristic would report "already-present" *after* doing
work.  The pre-gate skips the work.

`_filter_body_by_blocks` runs before `_try_deterministic_patch`.  If
the body's first block is a verbatim duplication of a previous
patch's insertion, that block is dropped from the body (the patch
still applies the rest).  If the entire body is a duplicate, the
caller marks the patch `already-present`.

`_verify_no_duplicate_insertion` runs *after* a heuristic produces
a candidate.  It compares pre/post line counts (modulo the body
itself) to catch the class where a heuristic spliced a new block
but also duplicated a pre-existing one.

`_try_blockquoted_patch` is a wrapper around `_try_deterministic_
patch` that additionally validates the result structurally.
Motivating case: thought-process blockquoted content that happens
to pass a heuristic (AST anchor, subset match) but produces a
file that fails `validate_content`.  The wrapper reverts to "no
patch" and records `structural_check: failed: ...` on the patch.
"""

import re
import sys
from collections import Counter
from pathlib import Path

from .core import _is_deletion_instruction, _looks_like_diff_body
from .patch_text import _split_body_into_blocks


# ===========================================================================
#  Insertion-filter helper
# ===========================================================================

def _filter_body_by_blocks(body, base_stripped_set, record_inserted):
    if not body.strip():
        return body, False
    blocks = _split_body_into_blocks(body)
    kept = []
    any_removed = False
    for block in blocks:
        non_context = [
            ln.strip() for ln in block.split("\n")
            if ln.strip() and ln.strip() not in base_stripped_set
        ]
        if non_context and all(
            ln in record_inserted for ln in non_context
        ):
            any_removed = True
            continue
        kept.append(block)
    return "\n".join(kept), any_removed


# ===========================================================================
#  Duplicate-insertion gate
# ===========================================================================

def _verify_no_duplicate_insertion(old_content, new_content, patch, path):
    old_lines = [ln.strip() for ln in old_content.splitlines()
                 if ln.strip()]
    new_lines = [ln.strip() for ln in new_content.splitlines()
                 if ln.strip()]
    old_counts = Counter(old_lines)
    new_counts = Counter(new_lines)

    body_lines = [ln.strip() for ln in patch.get("body", "").splitlines()
                  if ln.strip()]
    body_counts = Counter(body_lines)
    for line, count in body_counts.items():
        new_counts[line] -= count
        if new_counts[line] < 0:
            new_counts[line] = 0

    unexpected = {
        line: new_counts[line] - old_counts[line]
        for line in old_counts
        if new_counts[line] > old_counts[line]
    }
    if not unexpected:
        return True

    patch["duplicate_insertion"] = dict(unexpected)
    print(f"  [verify] {path}: patch idx={patch.get('idx')} produced "
          f"unexpected duplicate pre-existing line(s) "
          f"{dict(unexpected)}",
          file=sys.stderr)
    return False


# ===========================================================================
#  Duplicate-assignment pre-gate
# ===========================================================================

_DUP_ASSIGN_LINE_RE = re.compile(r'^[A-Za-z_$][\w$]*\s*=\s*[^=]')

_CONTINUATION_ENDINGS = (
    "(", "[", "{", "\\", ",", "+", "-", "*", "/", "=", "|", "&",
    " and", " or",
)


def _is_duplicate_assignment_insert(old_content, patch):
    body = patch.get("body") or ""
    body_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if len(body_lines) != 1:
        return False
    line = body_lines[0]
    if not _DUP_ASSIGN_LINE_RE.match(line):
        return False
    if line.endswith(_CONTINUATION_ENDINGS):
        return False
    return any(ln.strip() == line for ln in old_content.splitlines())


# ===========================================================================
#  Blockquote handling
# ===========================================================================

def _is_blockquoted_strong(b_idx, blocks_by_idx):
    b = blocks_by_idx.get(b_idx) if blocks_by_idx else None
    if b is None:
        return False
    if not b.in_blockquote:
        return False
    from .dedup import _has_strong_blockquote_attribution
    try:
        return _has_strong_blockquote_attribution(b)
    except Exception:
        return False


def _try_blockquoted_patch(content, patch, path, prior_insertions=None,
                            prior_scopes=None):
    from .patch_dispatch import _try_deterministic_patch
    from .validate_output import validate_content
    result = _try_deterministic_patch(
        content, patch, path,
        prior_insertions=prior_insertions,
        prior_scopes=prior_scopes,
    )
    if result is None:
        return None
    if result == content:
        return result
    trial = validate_content(result, path)
    if trial is not None:
        patch["structural_check"] = f"failed: {trial}"
        return None
    patch["structural_check"] = "ok"
    return result


# ===========================================================================
#  Prior-line recording
# ===========================================================================

def _record_single_line_anchor(patch, body, record_inserted,
                                prior_scopes=None, broader_ctx=None):
    if not isinstance(patch, dict):
        return
    body_lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(body_lines) != 1:
        return
    line = body_lines[0].strip()
    if not line:
        return
    record_inserted.add(line)

    if prior_scopes is None:
        return
    try:
        from .patch_scope_insert import _extract_scope_name_from_prose
    except ImportError:
        return
    scope = _extract_scope_name_from_prose(patch.get("context_before") or "")
    if not scope and broader_ctx:
        scope = _extract_scope_name_from_prose(broader_ctx)
    if scope:
        prior_scopes[line] = scope


# ===========================================================================
#  Already-present marking
# ===========================================================================

def _mark_already_present(patch, body, record_inserted,
                          prior_scopes=None, broader_ctx=None):
    patch["applied_by"] = "already-present"
    _record_single_line_anchor(patch, body, record_inserted,
                                prior_scopes=prior_scopes,
                                broader_ctx=broader_ctx)
