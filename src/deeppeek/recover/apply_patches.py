"""
apply_patches — pass 6 (apply patches, then sanity-check the result).

This module is now the *orchestrator*: it owns `pass6_apply_patches`
(the main loop over records/patches), the sanity gate, and the
per-record insertion filter.  The actual heuristics live in their
own modules (see the module map at the bottom of this docstring).

SPLIT RATIONALE
===============
`apply_patches.py` used to hold ~40 functions and had grown to
several thousand lines.  The split separates:

  · **Stable-ish infrastructure** — text primitives, scope detection,
    LLM diff application, diagnostics.  These rarely change; when they
    do, they change in one place and every consumer sees the update.

  · **Growing heuristics** — patch_heuristics.  New shapes get added
    here as they surface in the wild.  It's the module that grows.

  · **Dispatch and pairing** — patch_pairs, patch_dispatch.  These
    change when a new heuristic needs to slot into the chain, or when
    drift-tolerance needs to reach further.

  · **Orchestration** — this file.  The pass-6 main loop and its
    surrounding helpers.  Stable shape; opens upward only for new
    record-level concepts (a new op type, a new verification field).

BROADER CONTEXT RETRY (2026-11-01, FIX 5)
=========================================
When every deterministic heuristic fails on a patch, retry once with
`context_before` merged from the *adjacent blocks in the same record*.
The pg_panel_scrubs.py failure case: "before the `status.running`
field" prose was attached to the *import* block, not the *rotation*
block, so the rotation block's context_before had no anchor.  The
import block's context is one or two blocks back — within reach.
Merging it in lets `_try_insert_before_anchor` find the anchor it
needs.

This is cheap (one extra deterministic attempt, only on failure) and
catches the "the prose spans two blocks" shape without touching the
extractor's contract.

BLOCKQUOTED STRUCTURAL CHECK (FIX 6)
====================================
A blockquoted-orphan patch that survives `_has_strong_blockquote_
attribution` (exact prose + commit language) is the AI's explicit
commit, but it can still land in the wrong place — the
pg_panel_bindings.py case: a thought-process fragment landed inside
`_inpaintAction`'s body between `canvasB64` and `selectionB64`.
`validate_content` catches it post-write, but by then the corrupted
content has already been recorded as `applied` and the record is
demoted to LOW.  `_try_blockquoted_patch` trial-validates the
*just-applied* result inside pass 6; if the result is structurally
invalid, the attempt is refused and the patch is marked failed
instead of applied.

Only blockquoted-orphan patches get this treatment.  Normal patches
already benefit from the pass-8 verify; per-patch validation there
would refuse legitimate intermediate states (a patch that is
intentionally one half of a fragment closed by the next).  Blockquoted
orphans have no such excuse — they arrive as complete units.

MODULE MAP
==========
  patch_text          pure text primitives (leaf)
  patch_scope         scope detection, scoped replace, ellipsis splice
  patch_llm           LLM diff application
  patch_heuristics    insert / replace / fuzzy heuristics
  patch_pairs         replace-pair application + drift tolerance
  patch_diag          failure diagnostics
  patch_dispatch      deterministic-patch dispatcher + sanity attribution
  apply_patches       this file — pass6_apply_patches + re-exports
"""

from typing import Optional

from .core import (
    Block, FileRecord, FENCE, MAX_TOKENS_DIFF,
    _is_deletion_instruction,
    _looks_like_diff_body,
    _strip_diff_prefixes,
)
from .llm import LLMClient

# ---------------------------------------------------------------------------
# Re-export every name from the split modules so that
# `from deeppeek.recover.apply_patches import X` keeps working
# exactly as it did before the split.  The loop below mirrors the
# module-level namespace the pre-split file exposed; own-name
# definitions at the end of this file (`_filter_body_by_blocks`,
# `_sanity_gate`, `pass6_apply_patches`) override any sub-module
# names of the same spelling, because they are defined after the
# loop.
# ---------------------------------------------------------------------------

from . import patch_text, patch_scope, patch_llm, patch_heuristics
from . import patch_pairs, patch_diag, patch_dispatch

for _mod in (patch_text, patch_scope, patch_llm, patch_heuristics,
             patch_pairs, patch_diag, patch_dispatch):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_mod, _name)
del _mod, _name

# ---------------------------------------------------------------------------
#  Local wrappers for the diagnostic functions.
#
#  These shadow the re-exports above.  Two reasons:
#
#    1. The wrapper's "never raises, always returns a string"
#       contract is bound to its own module namespace: it must
#       resolve `_diagnose_patch_failure_inner` from
#       `apply_patches.__dict__`, not `patch_diag.__dict__`.  A
#       pure re-export would make any caller that monkey-patches
#       `apply_patches._diagnose_patch_failure_inner` (to prove the
#       wrapper catches raises) a silent no-op.
#
#    2. `pass6_apply_patches` below calls `_diagnose_patch_failure`
#       through a local name.  Keeping the local definition next to
#       the caller makes the wrapper's behaviour inspectable from
#       one place.
#
#  The actual diagnostic logic still lives in `patch_diag`; the
#  local `_inner` delegates to it.  No logic is duplicated.
# ---------------------------------------------------------------------------

def _diagnose_patch_failure_inner(patch, content, base):
    from .patch_diag import _diagnose_patch_failure_inner as _impl
    return _impl(patch, content, base)


def _diagnose_patch_failure(patch, content, base):
    try:
        return _diagnose_patch_failure_inner(patch, content, base)
    except Exception as e:
        return (f"diagnostic raised {type(e).__name__}: {e}; "
                f"patch could not be applied")


# ===========================================================================
#  Broader-context retry helper (FIX 5)
# ===========================================================================
#
# On deterministic failure, retry once with the context of the
# *previous* blocks in the same record.  This catches the shape where
# the anchor-prose ("before the `status.running` field") is attached
# to the block *before* the content block, not the content block
# itself.  See the module docstring.

def _broader_context_for(patch, blocks_by_idx, records, path):
    """Return `patch["context_before"]` merged with the previous
    blocks' prose from the same record.

    The merge is:
        <context of the previous 2 blocks, joined by newlines>
        <original context_before>

    Order matters: `_BEFORE_PROSE_RE` in patch_heuristics scans
    right-to-left for the most recent match, so the original
    context ends up *after* the merged prose.  If the original
    context already contains an anchor, the retry does not fire
    (see the caller — the broader retry only runs when the first
    attempt returns None).
    """
    rec = records.get(path)
    if rec is None:
        return patch.get("context_before", "")
    by_idx = sorted(rec.source_blocks)
    try:
        i = by_idx.index(patch["idx"])
    except ValueError:
        return patch.get("context_before", "")

    parts = []
    for j in range(max(0, i - 2), i):
        b = blocks_by_idx.get(by_idx[j])
        if b and b.context_before:
            parts.append(b.context_before)
    parts.append(patch.get("context_before", ""))
    return "\n".join(parts)


# ===========================================================================
#  Blockquoted structural check (FIX 6)
# ===========================================================================
#
# A blockquoted-orphan patch is the AI's explicit commit, but thought
# fragments can land in the wrong scope (pg_panel_bindings.py: a
# fragment landed inside `_inpaintAction`'s body between two const
# declarations, corrupting the function signature).  Trial-validate
# the result before accepting.

def _is_blockquoted_strong(b_idx, blocks_by_idx):
    """True iff block `b_idx` is a blockquoted orphan with strong
    attribution (exact prose + commit language).  These are the
    patches that get the structural trial check.
    """
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


def _try_blockquoted_patch(content, patch, path):
    """Apply the patch, then trial-validate.  If the result is
    structurally invalid, revert and return None.

    Used only for blockquoted-orphan patches (see
    `_is_blockquoted_strong`).  Non-blockquoted patches keep the
    plain `_try_deterministic_patch` path — they benefit from pass-8
    verify without the per-patch double-check, and legitimately
    produce intermediate states (a fragment closed by a later patch)
    that per-patch validation would refuse.
    """
    from .validate_output import validate_content
    result = _try_deterministic_patch(content, patch, path)
    if result is None:
        return None
    if result == content:
        # No-op; do not re-validate.
        return result
    trial = validate_content(result, path)
    if trial is not None:
        patch["structural_check"] = f"failed: {trial}"
        return None
    patch["structural_check"] = "ok"
    return result


# ===========================================================================
#  Insertion-filter helper
# ===========================================================================

def _filter_body_by_blocks(body, base_stripped_set, record_inserted):
    """Remove blocks whose non-context lines are ALL already inserted
    by prior pairs/patches in this record.

    Conservative rules:
      - Context lines (present in the base) are never filtered.
      - A block is only removed if EVERY non-context line is a
        duplicate.  A single new line keeps the whole block.
      - Structural integrity is preserved because filtering is per
        whole block, not per line.

    Returns (filtered_body, any_removed).
    """
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


def _sanity_gate(base, result):
    from .sanity import is_suspicious
    return is_suspicious(base, result)


# ===========================================================================
#  Main loop: pass 6
# ===========================================================================

def pass6_apply_patches(records, blocks_by_idx, llm):
    import sys
    from collections import Counter

    for path, rec in records.items():
        if rec.state not in ("SINGLE_FULL", "MULTI_FULL", "FULL_PLUS_PATCH"):
            continue
        if rec.is_deleted:
            continue
        if rec.last_full_idx is None:
            continue
        base_block = blocks_by_idx.get(rec.last_full_idx)
        if not base_block:
            rec.warnings.append("base block not found")
            rec.trust = "LOW"
            continue
        base = base_block.body
        content = base

        base_stripped_set = {
            line.strip() for line in base.split("\n") if line.strip()
        }
        record_inserted = set()

        trust_cap = "MEDIUM" if rec.dedup_reason == "last_full" else "HIGH"

        pair_records = []
        if rec.pairs:
            content, pair_records = _apply_pairs(content, rec.pairs)
            record_inserted |= _collect_inserted_lines(base, content)
            rec.verification["pair_records"] = pair_records

        all_deterministic = True
        all_ok = True
        for patch in rec.patches:
            original_body = patch["body"]
            filtered_body, was_filtered = _filter_body_by_blocks(
                original_body, base_stripped_set, record_inserted,
            )
            if was_filtered:
                patch["filtered_duplicate_lines"] = True
                if not filtered_body.strip():
                    patch["applied_by"] = "already-present"
                    print(
                        f"  [insert-filter] {path} "
                        f"idx={patch['idx']}: filtered to empty",
                        file=sys.stderr,
                    )
                    continue
                patch_to_apply = {**patch, "body": filtered_body}
                print(
                    f"  [insert-filter] {path} "
                    f"idx={patch['idx']}: filtered duplicate "
                    f"block(s)",
                    file=sys.stderr,
                )
            else:
                patch_to_apply = patch

            # FIX 6: route blockquoted-orphan patches through the
            # structural-check wrapper.  Normal patches use the
            # plain dispatcher.
            if _is_blockquoted_strong(patch["idx"], blocks_by_idx):
                result = _try_blockquoted_patch(
                    content, patch_to_apply, path)
            else:
                result = _try_deterministic_patch(
                    content, patch_to_apply, path)

            if result is not None:
                if result == content:
                    patch["applied_by"] = "already-present"
                else:
                    record_inserted |= _collect_inserted_lines(
                        content, result)
                    content = result
                    patch["applied_by"] = "deterministic"
                continue

            if was_filtered:
                if _is_blockquoted_strong(patch["idx"], blocks_by_idx):
                    result = _try_blockquoted_patch(
                        content, patch, path)
                else:
                    result = _try_deterministic_patch(
                        content, patch, path)
                if result is not None:
                    if result == content:
                        patch["applied_by"] = "already-present"
                    else:
                        record_inserted |= _collect_inserted_lines(
                            content, result)
                        content = result
                        patch["applied_by"] = "deterministic"
                        patch["filter_fallback"] = True
                        print(
                            f"  [insert-filter] {path} "
                            f"idx={patch['idx']}: filtered body "
                            f"failed, applied unfiltered",
                            file=sys.stderr,
                        )
                    continue

            if _patch_already_present(patch_to_apply["body"], content):
                patch["applied_by"] = "already-present"
                continue

            if _diff_already_applied(patch_to_apply["body"], content):
                patch["applied_by"] = "already-present"
                continue

            if _is_deletion_instruction(
                    patch_to_apply["body"],
                    patch.get("context_before", "")):
                if _deletion_already_applied(
                        patch_to_apply["body"], content):
                    patch["applied_by"] = "already-present"
                    continue

            # FIX 5: retry once with merged context before falling
            # through to the LLM.  The pg_panel_scrubs.py failure
            # case: the "before `status.running`" prose was attached
            # to the import block, not the rotation block.  The
            # merged context lets `_try_insert_before_anchor` see
            # the anchor.
            broader = _broader_context_for(
                patch_to_apply, blocks_by_idx, records, path)
            if broader != patch_to_apply.get("context_before", ""):
                broader_patch = {
                    **patch_to_apply, "context_before": broader}
                if _is_blockquoted_strong(patch["idx"], blocks_by_idx):
                    retry_result = _try_blockquoted_patch(
                        content, broader_patch, path)
                else:
                    retry_result = _try_deterministic_patch(
                        content, broader_patch, path)
                if retry_result is not None:
                    if retry_result == content:
                        patch["applied_by"] = "already-present"
                        patch["broader_context"] = True
                    else:
                        record_inserted |= _collect_inserted_lines(
                            content, retry_result)
                        content = retry_result
                        patch["applied_by"] = "deterministic"
                        patch["broader_context"] = True
                    continue

            all_deterministic = False
            if llm is not None:
                result = _try_llm_patch(
                    content, patch_to_apply, llm, path)
                if result is not None:
                    record_inserted |= _collect_inserted_lines(
                        content, result)
                    content = result
                    patch["applied_by"] = "llm"
                    continue

            patch["applied_by"] = "failed"
            try:
                patch["failure_reason"] = _diagnose_patch_failure(
                    patch, content, base)
            except Exception as _diag_exc:
                patch["failure_reason"] = (
                    f"diagnostic raised {type(_diag_exc).__name__}: "
                    f"{_diag_exc}; patch could not be applied")
            rec.warnings.append(
                f"patch idx={patch['idx']} could not be applied "
                f"({patch['failure_reason']})")

            _block_for_patch = blocks_by_idx.get(patch["idx"])
            if (_block_for_patch is not None
                    and _block_for_patch.attributed_by
                    == "body_header"):
                patch["applied_by"] = "skipped_weak_attribution"
                patch["attribution_demoted"] = True
                rec.warnings.append(
                    f"patch idx={patch['idx']} body_header "
                    f"attribution could not be applied — "
                    f"demoted to unattributed fragment "
                    f"(record not failed)")
            else:
                all_ok = False

        rec.verification["patches_applied"] = sum(
            1 for p in rec.patches
            if p.get("applied_by")
            not in ("failed", "skipped_weak_attribution"))
        rec.verification["pairs_applied"] = sum(
            1 for r in pair_records if r["applied_by"] == "deterministic")

        if pair_records:
            n_failed = sum(
                1 for r in pair_records if r["applied_by"] == "failed")
            if n_failed:
                rec.verification["pairs_incomplete"] = True
                reasons = [
                    r.get("reason", "unknown")
                    for r in pair_records if r["applied_by"] == "failed"
                ]
                rec.warnings.append(
                    f"{n_failed}/{len(pair_records)} replace pair(s) "
                    f"failed: " + "; ".join(reasons))

        suspicious, reason = _sanity_gate(base, content)
        if suspicious:
            if (_has_truncation_marker(content)
                    and not _has_truncation_marker(base)):
                reason = (
                    "truncation marker (e.g. `...`) present in result "
                    "but not in base; refusing to write truncated "
                    "content")
            rec.sanity_rejected = True
            content = base
            rec.trust = "LOW"
            rec.verification["sanity_reason"] = reason
            rec.warnings.append(f"sanity gate: {reason} — reverted to base")
            rec.verification["sanity_rejected"] = True
            print(f"  [sanity] {path}: {reason} — reverting to base",
                  file=sys.stderr)
        else:
            had_failure = (not all_ok
                           or rec.verification.get("pairs_incomplete", False))
            if had_failure:
                rec.trust = "LOW"
                rec.verification["patches_incomplete"] = (
                    rec.verification.get("patches_incomplete", False)
                    or not all_ok)
                rec.warnings.append(
                    "patch or pair application failed; file not written "
                    "to target under default --min-trust "
                    "(override with --min-trust LOW to force-write)")
                print(f"  [pass 6] {path}: incomplete application -> LOW "
                      f"trust", file=sys.stderr)
            else:
                rec.trust = trust_cap
                llm_count = sum(
                    1 for p in rec.patches
                    if p.get("applied_by") == "llm")
                if llm_count:
                    rec.verification["via_llm"] = True
                    rec.verification["llm_patch_count"] = llm_count

        rec.content = content

    total_patches = sum(len(r.patches) for r in records.values())
    total_pairs = sum(len(r.pairs) for r in records.values())
    total_applied = sum(
        sum(1 for p in r.patches
            if p.get("applied_by") in ("deterministic", "llm"))
        for r in records.values())
    total_pairs_applied = sum(
        r.verification.get("pairs_applied", 0) for r in records.values())
    total_noop = sum(
        sum(1 for p in r.patches if p.get("applied_by") == "already-present")
        for r in records.values())
    total_failed = total_patches - total_applied - total_noop
    files_with_failures = sum(
        1 for r in records.values()
        if any(p.get("applied_by") == "failed" for p in r.patches)
        or r.verification.get("pairs_incomplete"))
    n_sanity = sum(1 for r in records.values() if r.sanity_rejected)
    if total_patches or total_pairs:
        parts = []
        if total_patches:
            parts.append(f"{total_applied}/{total_patches} patch(es)")
        if total_pairs:
            parts.append(f"{total_pairs_applied}/{total_pairs} pair(s)")
        suffix = ""
        if total_failed:
            suffix = (f" ({total_failed} failed across "
                      f"{files_with_failures} file(s) — see warnings.txt)")
        if n_sanity:
            suffix += f" [{n_sanity} file(s) sanity-rejected]"
        if files_with_failures:
            suffix += " [fail-closed: LOW trust, not written to target]"
        print(f"  {' + '.join(parts)} applied{suffix}", file=sys.stderr)
        if total_noop:
            print(f"  {total_noop} patch(es) already-present "
                  f"(no-op, not failures)", file=sys.stderr)
