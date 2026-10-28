"""
apply_patches — pass 6 (apply patches, then sanity-check the result).

This module is the *orchestrator*: it owns `pass6_apply_patches`
(the main loop over records/patches), the sanity gate, and the
per-record insertion filter.  The actual heuristics live in their
own modules.

See the module docstring in the pre-fix version for the split
rationale, broader-context retry, and the blockquoted structural
check.  This version additionally:

  · Preserves the pre-existing file's preamble (leading docstring /
    comment block) when a FULL block replaces a pre-existing file and
    the block omits it (pg_state.py failure — block 94 discarded the
    module docstring).

  · Does NOT emit the "file not written to target under default
    --min-trust" warning.  That warning is misleading when pass 8.7
    reconciliation later promotes the record; `pass9_write` already
    emits the accurate "trust=LOW below --min-trust" line for records
    that stay LOW.

  · FIXED-POINT PATCH LOOP (2026-10-28).  The single-pass loop missed
    the case where a patch's anchor was supplied by a *later* patch in
    the same batch.  The loop now iterates over the pending set until
    a full pass adds nothing, then marks the survivors as `failed`.

  · DUPLICATE-INSERTION GATE (2026-10-28).  Every successful patch
    is post-checked against the pre-application content via
    `_verify_no_duplicate_insertion`.  A patch whose application
    added a duplicate copy of a **pre-existing** line outside its own
    body is reverted and marked `skipped_duplicate_insertion`.

  · STRUCTURAL-SANITY GATE (2026-10-28).  Every successful patch on a
    `.py` destination is post-checked via `_check_structural_sanity`.
    A patch whose application introduces a Python syntax error OR a
    module-level statement injection is reverted and marked
    `skipped_structural_damage`.

  · TERMINAL-STATUS PRESERVATION (2026-10-28).  Surviving `pending`
    patches with a terminal `applied_by` status are not relabelled
    `failed` by the follow-up loop.

  · PRIOR-INSERTIONS THREADING (2026-10-28).  `_try_deterministic_
    patch` (in `patch_dispatch.py`), `_try_blockquoted_patch`, and
    the fixed-point loop accept a `prior_insertions` set of stripped
    lines previously inserted this batch.  The anchor-after heuristic
    uses it as a fallback when the prose has no backtick anchor.

  · ALREADY-PRESENT ANCHORING (2026-10-28).  When a single-line body
    is `already-present` in base, the body line is added to
    `prior_insertions` so a subsequent "Add just after:" block can
    anchor to a pre-existing line.

  · PRE-GATE FOR DUPLICATE ASSIGNMENTS (2026-10-28b).  Before any
    heuristic runs, a patch whose body is a single `var = expr` line
    that already appears verbatim in the current content is marked
    `already-present` and skipped.

  · PRIOR-INSERTIONS SCOPE CONTEXT (2026-10-28e).  `_mark_already_
    present` and the duplicate-assignment pre-gate now also record
    the scope named by the Find block's *broader* prose (via
    `_broader_context_for`, which pulls the last two preceding
    blocks' `context_before`), keyed by the already-present line, in
    a `prior_scopes` dict that threads alongside `prior_insertions`
    into `_try_deterministic_patch`.

    The narrow `patch["context_before"]` for a Find block is usually
    just `"Find:"`.  The scope is often in the *previous* block's
    prose ("In `handlers/comfyui.py`, find the `comfy_generate`
    command decorator. Add:").  Reading broader context captures
    that scope, which the prior-insertion fallback then uses to
    disambiguate a repeated anchor.

    Motivating shape: comfyui.py idx=96/97.  idx=96's Find block
    anchors the line `output_name = params.get("output_name") or
    None`, which appears twice (in `_comfy_generate_impl` and
    `comfy_wait`).  Without broader context the extractor can't pick
    one; with it, `comfy_generate` is recorded, and
    `patch_anchor_insert._anchor_within_callee` resolves it to
    `_comfy_generate_impl`.  idx=97 then lands correctly.

DEPENDENCY: `_try_deterministic_patch` (in `patch_dispatch.py`) must
accept the same `prior_insertions=None` and `prior_scopes=None`
keywords that this module passes into it.  `_try_blockquoted_patch`
below threads both kwargs through unchanged.
"""

import re
from collections import Counter
from pathlib import Path
from typing import Optional

from .core import (
    Block, FileRecord, FENCE, MAX_TOKENS_DIFF,
    _is_deletion_instruction,
    _looks_like_diff_body,
    _strip_diff_prefixes,
)
from .llm import LLMClient

from . import patch_text, patch_scope, patch_llm, patch_heuristics
from . import patch_pairs, patch_diag, patch_dispatch

for _mod in (patch_text, patch_scope, patch_llm, patch_heuristics,
             patch_pairs, patch_diag, patch_dispatch):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_mod, _name)
del _mod, _name


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

def _broader_context_for(patch, blocks_by_idx, records, path):
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
    """Apply a blockquoted patch and validate the result structurally.

    `prior_scopes` is threaded unchanged to `_try_deterministic_patch`
    so prior-insertion anchors keep their recorded scopes.  See
    `_mark_already_present` and the module docstring,
    PRIOR-INSERTIONS SCOPE CONTEXT section.
    """
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


def _sanity_gate(base, result):
    from .sanity import is_suspicious
    return is_suspicious(base, result)


# ===========================================================================
#  Duplicate-insertion gate (Fix 6)
# ===========================================================================

def _verify_no_duplicate_insertion(old_content, new_content, patch, path):
    """Return True iff applying `patch` did not introduce an
    unexpected duplicate copy of a **pre-existing** line.
    """
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
          file=__import__("sys").stderr)
    return False


# ===========================================================================
#  Structural sanity gate (NEW 2026-10-28)
# ===========================================================================

def _check_structural_sanity(old_content, new_content, path):
    """Refuse a patch that introduces unfixable structural damage.

    Catches two failure modes on `.py` destinations:

      1. `ast.parse(new_content)` fails.

      2. A module-level statement keyword was added at column 0.

    Returns None for `.py` destinations that pass, or a short reason
    string.  Always returns None for non-Python destinations.
    """
    if not old_content:
        return None
    if Path(path).suffix.lower() != ".py":
        return None
    import ast
    try:
        ast.parse(new_content)
    except SyntaxError as e:
        return (f"patch introduced Python syntax error at "
                f"line {e.lineno}: {e.msg}")
    from .validate_output import _module_level_stmts_added
    added = _module_level_stmts_added(new_content, old_content)
    if added:
        return (f"patch injected module-level statement(s) at "
                f"column 0: {added}")
    return None


# ===========================================================================
#  Prior-line recording (NEW 2026-10-28e)
# ===========================================================================

def _record_single_line_anchor(patch, body, record_inserted,
                                prior_scopes=None, broader_ctx=None):
    """Record the body's single line (if any) as already-present.

    Also records the scope named by the patch's prose — or, when the
    narrow `context_before` doesn't name one, by the *broader* prose
    (`broader_ctx`, supplied by the caller via `_broader_context_for`).

    Broader-context recording is the load-bearing piece for the
    comfyui.py shape (blocks idx=96/97): the Find block's own prose
    is just `"Find:"`, but the two preceding blocks' prose names
    `comfy_generate` as the wider context.  With broader context,
    `prior_scopes[anchor]` carries that scope, which
    `patch_anchor_insert._anchor_within_callee` then resolves to
    `_comfy_generate_impl`.
    """
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
#  Already-present anchoring (2026-10-28 / 2026-10-28e)
# ===========================================================================

def _mark_already_present(patch, body, record_inserted,
                          prior_scopes=None, broader_ctx=None):
    """Mark a patch `already-present` and record its single line as
    an anchor for subsequent "Add just after:" blocks.

    `broader_ctx` (optional) is passed to `_record_single_line_anchor`
    as the fallback scope source when the narrow `context_before`
    doesn't name a scope.  See the module docstring, PRIOR-INSERTIONS
    SCOPE CONTEXT section.
    """
    patch["applied_by"] = "already-present"
    _record_single_line_anchor(patch, body, record_inserted,
                                prior_scopes=prior_scopes,
                                broader_ctx=broader_ctx)


# ===========================================================================
#  Duplicate-assignment pre-gate (2026-10-28b)
# ===========================================================================

_DUP_ASSIGN_LINE_RE = re.compile(r'^[A-Za-z_$][\w$]*\s*=\s*[^=]')

_CONTINUATION_ENDINGS = (
    "(", "[", "{", "\\", ",", "+", "-", "*", "/", "=", "|", "&",
    " and", " or",
)


def _is_duplicate_assignment_insert(old_content, patch):
    """True iff `patch` is a single-line assignment that already
    exists verbatim in `old_content`.
    """
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
#  Main loop: pass 6
# ===========================================================================

def pass6_apply_patches(records, blocks_by_idx, llm):
    import sys

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

        if rec.source_blocks:
            for _idx in rec.source_blocks:
                _pre = blocks_by_idx.get(_idx)
                if (_pre is not None
                        and _pre.classified_by == "preexisting"
                        and _pre.body):
                    base = _preserve_preamble(_pre.body, base)
                    break

        content = base

        base_stripped_set = {
            line.strip() for line in base.split("\n") if line.strip()
        }
        record_inserted = set()
        # NEW (2026-10-28e): prior-insertion scope context.  Maps a
        # stripped line to the scope name extracted from the Find
        # block's broader prose.  Consumed by
        # `_try_insert_after_anchor`'s prior-insertion fallback when
        # the anchor is repeated in the base.
        prior_scopes: dict = {}

        trust_cap = "MEDIUM" if rec.dedup_reason == "last_full" else "HIGH"

        pair_records = []
        if rec.pairs:
            content, pair_records = _apply_pairs(content, rec.pairs)
            record_inserted |= _collect_inserted_lines(base, content)
            rec.verification["pair_records"] = pair_records

        def _broader_for(p):
            return _broader_context_for(p, blocks_by_idx, records, path)

        # ------------------------------------------------------------------
        # FIXED-POINT PATCH LOOP
        # ------------------------------------------------------------------
        all_ok = True
        pending = list(rec.patches)

        while pending:
            applied_this_pass = 0
            still_pending = []
            for patch in pending:
                if _is_duplicate_assignment_insert(content, patch):
                    patch["applied_by"] = "already-present"
                    patch["duplicate_assignment"] = True
                    print(
                        f"  [dup-assign] {path} idx={patch['idx']}: "
                        f"single-line assignment already present; "
                        f"skipping",
                        file=sys.stderr,
                    )
                    # Anchor the (already-present) line for subsequent
                    # "Add just after:" blocks.  See _record_single_line_anchor.
                    _body = patch.get("body") or ""
                    _record_single_line_anchor(
                        patch, _body, record_inserted,
                        prior_scopes=prior_scopes,
                        broader_ctx=_broader_for(patch),
                    )
                    continue

                original_body = patch["body"]
                filtered_body, was_filtered = _filter_body_by_blocks(
                    original_body, base_stripped_set, record_inserted,
                )
                if was_filtered:
                    patch["filtered_duplicate_lines"] = True
                    if not filtered_body.strip():
                        _mark_already_present(
                            patch, original_body, record_inserted,
                            prior_scopes=prior_scopes,
                            broader_ctx=_broader_for(patch),
                        )
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

                if _is_blockquoted_strong(patch["idx"], blocks_by_idx):
                    result = _try_blockquoted_patch(
                        content, patch_to_apply, path,
                        prior_insertions=record_inserted,
                        prior_scopes=prior_scopes,
                    )
                else:
                    result = _try_deterministic_patch(
                        content, patch_to_apply, path,
                        prior_insertions=record_inserted,
                        prior_scopes=prior_scopes,
                    )

                if result is not None and result != content:
                    if not _verify_no_duplicate_insertion(
                            content, result, patch, path):
                        patch["applied_by"] = "skipped_duplicate_insertion"
                        continue
                    _struct_err = _check_structural_sanity(
                        content, result, path)
                    if _struct_err is not None:
                        patch["applied_by"] = "skipped_structural_damage"
                        patch["failure_reason"] = _struct_err
                        print(
                            f"  [struct-gate] {path} idx={patch['idx']}: "
                            f"{_struct_err}",
                            file=sys.stderr,
                        )
                        continue
                    record_inserted |= _collect_inserted_lines(
                        content, result)
                    content = result
                    patch["applied_by"] = "deterministic"
                    applied_this_pass += 1
                    continue

                if result == content:
                    _mark_already_present(
                        patch, original_body, record_inserted,
                        prior_scopes=prior_scopes,
                        broader_ctx=_broader_for(patch),
                    )
                    continue

                if was_filtered:
                    if _is_blockquoted_strong(patch["idx"], blocks_by_idx):
                        result = _try_blockquoted_patch(
                            content, patch, path,
                            prior_insertions=record_inserted,
                            prior_scopes=prior_scopes,
                        )
                    else:
                        result = _try_deterministic_patch(
                            content, patch, path,
                            prior_insertions=record_inserted,
                            prior_scopes=prior_scopes,
                        )
                    if result is not None and result != content:
                        if not _verify_no_duplicate_insertion(
                                content, result, patch, path):
                            patch["applied_by"] = \
                                "skipped_duplicate_insertion"
                            continue
                        _struct_err = _check_structural_sanity(
                            content, result, path)
                        if _struct_err is not None:
                            patch["applied_by"] = \
                                "skipped_structural_damage"
                            patch["failure_reason"] = _struct_err
                            continue
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
                        applied_this_pass += 1
                        continue
                    if result == content:
                        _mark_already_present(
                            patch, original_body, record_inserted,
                            prior_scopes=prior_scopes,
                            broader_ctx=_broader_for(patch),
                        )
                        continue

                if _patch_already_present(patch_to_apply["body"], content):
                    _mark_already_present(
                        patch, patch_to_apply["body"], record_inserted,
                        prior_scopes=prior_scopes,
                        broader_ctx=_broader_for(patch),
                    )
                    continue

                if _diff_already_applied(patch_to_apply["body"], content):
                    _mark_already_present(
                        patch, patch_to_apply["body"], record_inserted,
                        prior_scopes=prior_scopes,
                        broader_ctx=_broader_for(patch),
                    )
                    continue

                if _is_deletion_instruction(
                        patch_to_apply["body"],
                        patch.get("context_before", "")):
                    if _deletion_already_applied(
                            patch_to_apply["body"], content):
                        _mark_already_present(
                            patch, patch_to_apply["body"],
                            record_inserted,
                            prior_scopes=prior_scopes,
                            broader_ctx=_broader_for(patch),
                        )
                        continue

                broader = _broader_context_for(
                    patch_to_apply, blocks_by_idx, records, path)
                if broader != patch_to_apply.get("context_before", ""):
                    broader_patch = {
                        **patch_to_apply, "context_before": broader}
                    if _is_blockquoted_strong(
                            patch["idx"], blocks_by_idx):
                        retry_result = _try_blockquoted_patch(
                            content, broader_patch, path,
                            prior_insertions=record_inserted,
                            prior_scopes=prior_scopes,
                        )
                    else:
                        retry_result = _try_deterministic_patch(
                            content, broader_patch, path,
                            prior_insertions=record_inserted,
                            prior_scopes=prior_scopes,
                        )
                    if retry_result is not None and retry_result != content:
                        if not _verify_no_duplicate_insertion(
                                content, retry_result, patch, path):
                            patch["applied_by"] = \
                                "skipped_duplicate_insertion"
                            continue
                        _struct_err = _check_structural_sanity(
                            content, retry_result, path)
                        if _struct_err is not None:
                            patch["applied_by"] = \
                                "skipped_structural_damage"
                            patch["failure_reason"] = _struct_err
                            continue
                        record_inserted |= _collect_inserted_lines(
                            content, retry_result)
                        content = retry_result
                        patch["applied_by"] = "deterministic"
                        patch["broader_context"] = True
                        applied_this_pass += 1
                        continue
                    if retry_result == content:
                        _mark_already_present(
                            patch, patch_to_apply["body"],
                            record_inserted,
                            prior_scopes=prior_scopes,
                            broader_ctx=_broader_for(patch),
                        )
                        continue

                if llm is not None:
                    result = _try_llm_patch(
                        content, patch_to_apply, llm, path)
                    if result is not None and result != content:
                        if not _verify_no_duplicate_insertion(
                                content, result, patch, path):
                            patch["applied_by"] = \
                                "skipped_duplicate_insertion"
                            continue
                        record_inserted |= _collect_inserted_lines(
                            content, result)
                        content = result
                        patch["applied_by"] = "llm"
                        applied_this_pass += 1
                        continue

                still_pending.append(patch)

            if applied_this_pass == 0:
                break
            pending = still_pending

        # Mark surviving pending patches as failed with diagnostics.
        for patch in pending:
            if patch.get("applied_by") in (
                "already-present",
                "skipped_duplicate_insertion",
                "skipped_structural_damage",
            ):
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
            not in ("failed", "skipped_weak_attribution",
                    "skipped_duplicate_insertion",
                    "skipped_structural_damage"))
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
                           or rec.verification.get(
                               "pairs_incomplete", False))
            if had_failure:
                rec.trust = "LOW"
                rec.verification["patches_incomplete"] = (
                    rec.verification.get("patches_incomplete", False)
                    or not all_ok)
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
        if any(p.get("applied_by") in
               ("failed", "skipped_duplicate_insertion",
                "skipped_structural_damage")
               for p in r.patches)
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
