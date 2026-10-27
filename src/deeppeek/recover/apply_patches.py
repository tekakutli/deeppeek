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
    that stay LOW.  Removing the pass-6 warning eliminates the stale-
    warning class the pg_panel_bindings.py + pg_panel_scrubs.py
    failures exhibited.
"""

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


def _try_blockquoted_patch(content, patch, path):
    from .validate_output import validate_content
    result = _try_deterministic_patch(content, patch, path)
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

        # NEW: preserve the pre-existing file's preamble.
        #
        # When a FULL block replaces a pre-existing file and omits the
        # pre-existing preamble (leading docstring / comment block),
        # prepend it.  The pg_state.py failure: block 94 was
        # `STATE_JS = r\"\"\"...\"\"\"` (the constant body only), the
        # pre-existing file had a 17-line module docstring above it,
        # and `pass9_write` wrote the block body verbatim — the
        # docstring was lost.
        #
        # `_preserve_preamble` is a no-op when either side has no
        # preamble or when the block has its own (explicit new
        # docstring).  Only fires on the specific "block omits the
        # pre-existing preamble" shape.
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
                # NOTE: no "file not written" warning here.  Pass 9
                # emits the accurate "trust=LOW below --min-trust"
                # line for records that actually stay LOW; if pass
                # 8.7 reconciliation promotes the record, no warning
                # is correct (the file IS written).  Emitting the
                # warning here unconditionally produced the stale-
                # warning class in the pg_panel_bindings.py and
                # pg_panel_scrubs.py failures.
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
