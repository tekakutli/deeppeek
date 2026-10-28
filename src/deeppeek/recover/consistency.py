"""
recover.consistency — Pass 8.7d: verify `already-present` claims.

Motivation
==========
`pass6_apply_patches` marks a patch `already-present` when
`_try_deterministic_patch` returns its input unchanged.  The
dispatcher returns unchanged content in three situations:

  1. The body genuinely is already in base (a reprint; a splice whose
     target has no diff; a deletion whose lines are already gone).
  2. A heuristic returned `base` on a legitimate no-op transform
     (object-literal splice whose merge produces no net change).
  3. A heuristic *short-circuited* (`_is_already_applied` or
     `_patch_already_present` fired on a token-equivalence match) even
     though the body's actual content is **not** in base.

The first two are correct.  The third is a bug — the manifest says
"already-present" and `write.preexisting_unmodified` then skips the
file, but the file's contents never gained the patch's contribution.

Motivating case (comfyui.py, 2026-10-28 run):

    Block idx=652 body: a rewritten `comfy_set_layer_pixels` whose
    first ~50 lines (the PNG-decode boilerplate) are byte-identical
    to the pre-existing method, and whose last ~10 lines
    (delete-and-recreate via `_collect`) are entirely new.

    `_is_already_applied` computed a contiguous run of ~50 of 60
    lines, but the `_max_contiguous_run / total >= 0.95` gate should
    have refused the claim — the run was under threshold.  Some other
    heuristic returned `base` for an unrelated reason (see the git
    history of `apply_patches._try_deterministic_patch_inner`), and
    the dispatcher marked the patch `already-present`.

    `write.preexisting_unmodified` then saw all patches in
    NO_CHANGE_STATUSES, treated the file as untouched, and skipped
    it.  The user never saw the fix.  The manifest reported
    `patches_applied: 1` (because that counter used an
    "everything-not-failed" filter) and `written: false`.  No
    residual entry either, because `_block_outcome` returns None for
    `already-present`.

Fix
===
Pass 8.7d re-verifies every `already-present` claim.  For each such
patch, check whether the body's content is actually in `rec.content`
at ≥95% line-equivalence.  If not, flip the patch to `failed` with
a specific reason and demote the record.  Reconcile then sees the
corrected status, tries to exclude the failed patch, and either
promotes the record (rest of the record writes cleanly) or leaves it
LOW (target preserved, residual surfaces the block).

The pass is deliberately narrow: it only inspects `already-present`
patches.  Applied-but-missing patches (`deterministic` / `llm` whose
body isn't in the final content) are already covered by pass 8.7c,
which warns without demoting (because supersession is common and
benign — see that pass's docstring).

Skips
=====
Three patch shapes are skipped to avoid false positives:

  * Empty body: nothing to verify.
  * Diff body: the semantics of "already-present" for a diff is "the
    `+` lines are in content and the `-` lines are gone".  This is
    already checked by `_diff_already_applied` at apply time, and
    neither the raw diff text nor the pure-addition subset appears in
    base as contiguous content.  Verifying literally would flip every
    correctly-applied diff patch.
  * Deletion-instruction body: the body being *absent* is the success
    condition.  Already checked by `_deletion_already_applied`.

Test coverage: `tests/recover/test_consistency.py`.
"""

from __future__ import annotations

import sys
from typing import Optional

from .core import (
    FileRecord,
    _is_deletion_instruction,
    _looks_like_diff_body,
)


_ALREADY_PRESENT = "already-present"
_LINE_EQUIVALENCE_THRESHOLD = 0.95


def _body_content_present(body: str, content: str) -> bool:
    """True iff `body`'s content can reasonably be said to be in
    `content`.

    Two tiers:
      * Exact substring (after surrounding-whitespace strip).
      * Line-by-line stripped membership at 95%.

    The 95% threshold is calibrated against the motivating bug: the
    comfyui.py patch had ~50 of 60 lines matching base by strip, so
    0.83 line-equivalence — well under 0.95, correctly flagged as
    not-present.  A legitimate reprint with a reflowed blank line or
    a trailing-comment addition typically loses at most one or two
    lines out of dozens and stays above 0.95.
    """
    stripped = body.strip()
    if not stripped:
        return True
    if stripped in content:
        return True
    body_lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not body_lines:
        return True
    content_lines = {ln.strip() for ln in content.splitlines()
                     if ln.strip()}
    matched = sum(1 for ln in body_lines if ln in content_lines)
    return matched / len(body_lines) >= _LINE_EQUIVALENCE_THRESHOLD


def pass8_7d_verify_already_present(
        records: dict[str, FileRecord],
        blocks_by_idx: Optional[dict] = None,
        verbose: bool = False) -> int:
    """Verify every `already-present` patch's claim; flip false
    positives to `failed`.

    `blocks_by_idx` is accepted for API parity with the other passes
    but is not required — the check only inspects `rec.content`, which
    is already populated by the time this pass runs.

    Returns the number of patches flipped to `failed`.
    """
    n_flipped = 0
    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.is_deleted:
            continue
        if not rec.patches:
            continue

        for patch in rec.patches:
            if patch.get("applied_by") != _ALREADY_PRESENT:
                continue
            body = patch.get("body") or ""
            if not body.strip():
                continue
            if _looks_like_diff_body(body):
                continue
            if _is_deletion_instruction(
                    body, patch.get("context_before", "") or ""):
                continue
            if _body_content_present(body, rec.content):
                continue

            idx = patch.get("idx")
            patch["applied_by"] = "failed"
            patch["failure_reason"] = (
                "marked already-present but the body's content is not "
                "in the current file (the dispatcher returned unchanged "
                "content from a token-equivalence match that does not "
                "correspond to the patch's actual bytes; see "
                "consistency.py's module docstring)"
            )
            rec.verification.setdefault(
                "already_present_unverified", []).append(idx)
            rec.warnings.append(
                f"already-present claim for patch idx={idx} was "
                f"contradicted by the file content; marked failed "
                f"(the patch's contribution is not in the target)"
            )
            rec.trust = "LOW"
            n_flipped += 1

            if verbose:
                print(f"  [8.7d] {path}: patch idx={idx} was marked "
                      f"already-present but its content is absent; "
                      f"flipped to failed", file=sys.stderr)

    return n_flipped
