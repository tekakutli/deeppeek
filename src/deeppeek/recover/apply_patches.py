"""
apply_patches — pass 6 (apply patches, then sanity-check the result).

This module is the *orchestrator*: it owns `pass6_apply_patches`
(the main loop over records/patches), the broader-context retry
helper, replace-intent detection, and the pass-6 diagnostic
wrappers.

The gates it consults live in sibling modules:

  · `pass6_sanity.py` — structural-damage and sanity rejection
    (`_body_is_wellformed`, `_check_structural_sanity`,
    `_enrich_structural_damage`, `_sanity_gate`).

  · `pass6_gates.py` — insertion filters, dedup gates, blockquote
    policy, anchor recording (`_filter_body_by_blocks`,
    `_verify_no_duplicate_insertion`,
    `_is_duplicate_assignment_insert`, `_is_blockquoted_strong`,
    `_try_blockquoted_patch`, `_record_single_line_anchor`,
    `_mark_already_present`).

The actual patch heuristics live in `patch_dispatch` /
`patch_heuristics` / `patch_scope` / `patch_pairs` / `patch_llm`.
Every name from those modules is re-exported via the `_mod` loop
below, so `from deeppeek.recover.apply_patches import X` keeps
resolving for every X that lived here pre-split.

STRUCTURAL SANITY GATE (2026-10-29, revised)
============================================
`_check_structural_sanity` validates embedded content in `.py` files
(JS/HTML inside triple-quoted string literals) — the pg_panel failure
class where outer Python parses fine but inner JS is broken.

For `.js`/`.ts`/`.html` destinations the gate is **conditional on the
patch body being well-formed**.  A patch body that is itself
unbalanced (e.g. `if (cond) { doA();` with no closer) is *user-
provided truncation*: the correct behaviour is to apply it and let
pass 8's `try_repair` produce a fix suggestion for the user.  A patch
body that is balanced but whose result is not is *heuristic misfire*:
the boundary was wrong, and we reject so the next heuristic gets a
chance.

DUPLICATE-NESTING GATE (2026-11-05)
===================================
`_check_structural_sanity` also rejects patches that introduce the
same HTML element (same `tag` + same `class`) nested inside itself,
when the base did not already have that nesting.  Motivating case:
pg_base.py, where `_try_region_replace` spliced a 17-line body over
a 3-line `<input>` window, producing `<div class="field">` nested
inside another `<div class="field">`.

PASS-6 FAILED-STATUS ANNOTATION (2026-10-29)
============================================
After the apply loop, if the record's *final* content is structurally
invalid AND the base was structurally clean AND no applied patch had
an unbalanced body, every patch marked `deterministic` is downgraded
to `failed`.  Reconcile (pass 8.7) sees the failed status and its
`_find_minimum_exclusions` can propose exclusions.

SCOPE-HINT PROPAGATION (2026-11-05)
===================================
`pass6_apply_patches` copies each trailing block's `scope_hint` onto
the patch dict it builds.  Without it, `_try_insert_into_named_scope`
— which resolves the target scope from prose — had no way to find the
scope for block 634's shape (prose: "And in every failure return:",
H2: "## Fix 3 — surface per-command errors in `handle_inpaint`").

PATH THREADING (2026-10-29)
===========================
`_apply_pairs` is called with `path=path` so pairs get AST snapping +
structural validation on their splice.

TOOL-BUG SURFACING (2026-10-29)
===============================
When `_try_deterministic_patch` catches a `_TOOL_BUG_EXCEPTIONS`
raise it records `patch["tool_bug"] = "..."` and returns None
(unchanged contract).  The failure-diagnostic loop in
`pass6_apply_patches` now checks for that field first.

PRE-EXISTING SHORT-CIRCUIT (2026-11-05)
=======================================
A pre-existing record with no patches and no pairs has nothing for
pass 6 to do: the base IS the content, no heuristics can place
anything, and the sanity gate costs a string encode + compare.  On
large `--apply-to` trees (hundreds of untouched pre-existing files)
that overhead dominated the run.

`pass6_apply_patches` now short-circuits such records before the
pending-patch loop: `content = base`, `verification["preexisting_
unchanged"] = True`, and `continue`.  Pass 8 skips structural
validation for records carrying that flag (`verify.pass8_verify`).
Pass 9's `preexisting_unmodified` branch already handles them.

The flag is a *derived* fact — computed in one place (pass 6), read
in one place per downstream pass.  It does not depend on any field
written by a later pass.  That is what makes it safe where the
pruning loop was not: `preexisting_base` was set by pass 5, but the
pruning lived *before* pass 5 and consumed it, so the check always
saw False.

STRUCTURAL DIVERGENCE ENRICHMENT (2026-11-05)
=============================================
Every `_check_structural_sanity` rejection site now calls
`_enrich_structural_damage`, which appends the AST-level divergence
`structural_diff` observed to the parser-level `failure_reason`.

The parser-level reason names *that* the result is malformed
("patch produced structurally-invalid .js content").  The structural
divergence names *what* changed beyond what the patch declared
("removed 'function _pickMaskAction'").  Both together tell the
operator whether the tool silently dropped a sibling or introduced
an element that shouldn't be there.

Three call sites because the patch loop advances state between
them: the filtered-body path, the unfiltered-fallback path, and the
broader-context-retry path each need the exact `(base, attempted,
patch_body)` tuple `_check_structural_sanity` was called with, or
`structural_diff` compares the wrong things.

Prior docstring content (unchanged):
- REPLACE-INTENT DETECTION
- Blockquoted structural check
- Duplicate-insertion gate
- Fixed-point patch loop
- Prior-line recording / prior-insertions scope context
- Broader-context retry
"""

import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from .core import (
    Block, FileRecord, FENCE, MAX_TOKENS_DIFF,
    _is_deletion_instruction,
    _looks_like_diff_body,
    _strip_diff_prefixes,
    APPLIED_STATUSES,
    NO_CHANGE_STATUSES,
    FAILED_STATUSES,
)
from .llm import LLMClient
from .patch_replace_intent import detect_replace_intent

from . import patch_text, patch_scope, patch_llm, patch_heuristics
from . import patch_pairs, patch_diag, patch_dispatch

for _mod in (patch_text, patch_scope, patch_llm, patch_heuristics,
             patch_pairs, patch_diag, patch_dispatch):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_mod, _name)
del _mod, _name

# Gates and helpers (extracted 2026-11-05).  Re-exported so
# `from deeppeek.recover.apply_patches import X` keeps resolving for
# every X that lived here pre-split.
from .pass6_sanity import (
    _body_is_wellformed,
    _check_structural_sanity,
    _enrich_structural_damage,
    _sanity_gate,
)
from .pass6_gates import (
    _DUP_ASSIGN_LINE_RE,
    _CONTINUATION_ENDINGS,
    _is_duplicate_assignment_insert,
    _filter_body_by_blocks,
    _verify_no_duplicate_insertion,
    _is_blockquoted_strong,
    _try_blockquoted_patch,
    _record_single_line_anchor,
    _mark_already_present,
)


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
#  Broader-context retry helper
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
#  Replace-intent application
# ===========================================================================

def _try_replace_intent(content: str, patch, path: str,
                        record_inserted: set):
    """Attempt to apply `patch` as a region replacement."""
    body = patch.get("body") or ""
    intent = detect_replace_intent(content, body)
    if intent is None:
        return None

    start_line, end_line, replacement_body = intent
    base_lines = content.split("\n")

    if start_line < 0 or end_line > len(base_lines):
        return None
    if start_line >= end_line:
        return None

    from .patch_bounds import (
        try_boundary_pairs, try_ast_snapped_boundaries, splice_region,
    )
    body_lines = replacement_body.split("\n")

    candidates = [(start_line, end_line)]
    snapped = try_ast_snapped_boundaries(
        content, path, start_line, end_line)
    if snapped is not None and snapped != (start_line, end_line):
        candidates.append(snapped)
    candidates.extend(try_boundary_pairs(
        start_line, end_line, len(base_lines)))

    for trial_start, trial_end in candidates:
        result = splice_region(
            content, trial_start, trial_end, body_lines, path)
        if result is None:
            continue
        if result == content:
            return content
        if not _verify_no_duplicate_insertion(
                content, result, patch, path):
            continue
        patch["replace_intent"] = {
            "start_line": trial_start,
            "end_line": trial_end,
            "replaced_lines": trial_end - trial_start,
        }
        record_inserted |= _collect_inserted_lines(content, result)
        return result
    return None


# ===========================================================================
#  Main loop: pass 6
# ===========================================================================

def pass6_apply_patches(records, blocks_by_idx, llm):
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

        # NEW (2026-11-05): pre-existing record with nothing to
        # apply.  Skip the sanity gate and the pending-patch loop —
        # they have no work to do and their cost (string encode for
        # the sanity gate, fixpoint loop setup) is nontrivial on
        # large trees.  Set `content` so pass 9 can identify this
        # record as preexisting_unmodified, and set a verification
        # flag pass 8 consults to skip validation.
        if (rec.preexisting_base
                and not rec.patches
                and not rec.pairs):
            rec.content = base
            rec.verification["preexisting_unchanged"] = True
            rec.verification["patches_applied"] = 0
            rec.verification["patches_noop"] = 0
            rec.verification["patches_failed"] = 0
            rec.verification["pairs_applied"] = 0
            continue

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
        prior_scopes: dict = {}

        trust_cap = "MEDIUM" if rec.dedup_reason == "last_full" else "HIGH"

        pair_records = []
        if rec.pairs:
            content, pair_records = _apply_pairs(
                content, rec.pairs, path=path)
            record_inserted |= _collect_inserted_lines(base, content)
            rec.verification["pair_records"] = pair_records

        def _broader_for(p):
            return _broader_context_for(p, blocks_by_idx, records, path)

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
                    _body = patch.get("body") or ""
                    _record_single_line_anchor(
                        patch, _body, record_inserted,
                        prior_scopes=prior_scopes,
                        broader_ctx=_broader_for(patch),
                    )
                    continue

                original_body = patch["body"]

                _ri_result = _try_replace_intent(
                    content, patch, path, record_inserted)
                if _ri_result is not None:
                    if _ri_result == content:
                        _mark_already_present(
                            patch, original_body, record_inserted,
                            prior_scopes=prior_scopes,
                            broader_ctx=_broader_for(patch),
                        )
                        continue
                    content = _ri_result
                    patch["applied_by"] = "deterministic"
                    applied_this_pass += 1
                    print(
                        f"  [replace-intent] {path} idx={patch['idx']}: "
                        f"replaced lines "
                        f"{patch['replace_intent']['start_line']}.."
                        f"{patch['replace_intent']['end_line']}",
                        file=sys.stderr,
                    )
                    continue

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
                        content, result, path,
                        patch_body=patch_to_apply["body"])
                    if _struct_err is not None:
                        patch["applied_by"] = "skipped_structural_damage"
                        patch["failure_reason"] = _enrich_structural_damage(
                            _struct_err, content, result, path,
                            patch_to_apply["body"],
                        )
                        print(
                            f"  [struct-gate] {path} idx={patch['idx']}: "
                            f"{patch['failure_reason']}",
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
                            content, result, path,
                            patch_body=patch["body"])
                        if _struct_err is not None:
                            patch["applied_by"] = \
                                "skipped_structural_damage"
                            patch["failure_reason"] = _enrich_structural_damage(
                                _struct_err, content, result, path,
                                patch["body"],
                            )
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
                            content, retry_result, path,
                            patch_body=broader_patch["body"])
                        if _struct_err is not None:
                            patch["applied_by"] = \
                                "skipped_structural_damage"
                            patch["failure_reason"] = _enrich_structural_damage(
                                _struct_err, content, retry_result, path,
                                broader_patch["body"],
                            )
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

        for patch in pending:
            if patch.get("applied_by") in (
                "already-present",
                "skipped_duplicate_insertion",
                "skipped_structural_damage",
            ):
                continue
            patch["applied_by"] = "failed"
            if patch.get("tool_bug"):
                patch["failure_reason"] = (
                    f"deeppeek bug: {patch['tool_bug']}")
            else:
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

        _tool_bug_patches = [p for p in rec.patches if p.get("tool_bug")]
        if _tool_bug_patches:
            rec.verification["tool_bug"] = [
                {"idx": p["idx"], "error": p["tool_bug"]}
                for p in _tool_bug_patches
            ]
            for p in _tool_bug_patches:
                rec.warnings.append(
                    f"deeppeek bug: patch idx={p['idx']} failed "
                    f"with {p['tool_bug']} — this is a tooling "
                    f"failure, not a data-shape failure; report the "
                    f"exception type and the message to deeppeek")

        from .patch_bounds import structural_valid as _structural_valid
        applied_patches = [p for p in rec.patches
                           if p.get("applied_by") == "deterministic"]
        any_unbalanced_body = any(
            not _body_is_wellformed(p.get("body", ""), path)
            for p in applied_patches
        )
        if (content
                and not _structural_valid(content, path)
                and (not base or _structural_valid(base, path))
                and not any_unbalanced_body):
            for p in applied_patches:
                p["applied_by"] = "failed"
                p["failure_reason"] = (
                    "patch produced structurally-invalid content "
                    "(detected in pass 6; see validate_content for "
                    "the specific parse error)")
            rec.trust = "LOW"
            rec.verification["patches_incomplete"] = True
            print(
                f"  [struct-annotation] {path}: content is "
                f"structurally invalid; deterministic patches "
                f"downgraded to failed so reconcile can exclude",
                file=sys.stderr,
            )
            all_ok = False

        rec.verification["patches_applied"] = sum(
            1 for p in rec.patches
            if p.get("applied_by") in APPLIED_STATUSES)
        rec.verification["patches_noop"] = sum(
            1 for p in rec.patches
            if p.get("applied_by") in NO_CHANGE_STATUSES)
        rec.verification["patches_failed"] = sum(
            1 for p in rec.patches
            if p.get("applied_by") in FAILED_STATUSES)
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
            if p.get("applied_by") in APPLIED_STATUSES)
        for r in records.values())
    total_pairs_applied = sum(
        r.verification.get("pairs_applied", 0) for r in records.values())
    total_noop = sum(
        sum(1 for p in r.patches
            if p.get("applied_by") in NO_CHANGE_STATUSES)
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


def attribute_sanity_failure(rec, blocks_by_idx, validate_fn=None):
    if validate_fn is None:
        from .validate_output import validate_content as validate_fn

    if not rec.patches:
        return None
    base_block = (blocks_by_idx.get(rec.last_full_idx)
                  if rec.last_full_idx is not None else None)
    if base_block is None:
        return None
    base_content = base_block.body
    if base_content is None:
        return None

    current = base_content
    for patch_dict in rec.patches:
        applied_by = patch_dict.get("applied_by")
        if applied_by == "already-present":
            continue
        if applied_by == "failed":
            continue
        if applied_by != "deterministic":
            return None
        result = _try_deterministic_patch(current, patch_dict, rec.path)
        if result is None:
            continue
        if result == current:
            continue
        current = result
        if validate_fn(current, rec.path) is not None:
            return patch_dict["idx"]
    return None
