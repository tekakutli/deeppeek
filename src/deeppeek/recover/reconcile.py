"""
recover.reconcile — Pass 8.7: reconcile fail-closed records by
excluding the minimum set of patches/pairs that caused the failure.

See the pre-fix module docstring for the motivating cases and the
"reconciled to base is not a rescue" invariant.

CUMULATIVE-CONTEXT DIAGNOSTICS (2026-10-29; refined 2026-11-05)
================================================================
`_diagnose_exclusion` used to compare the excluded patch's body
against `records` (other files) only. When a patch was excluded for
a reason that depends on *state the record is going to have*, that
comparison produced misleading messages.

Motivating case: `inpaint_server.py` in the 2026-10-29 run. Two
patches, applied in order:

  idx 633: adds `errors: []` to `captured = {...}` and records
           `captured["errors"].append(...)` in the progress handler.
  idx 634: a *multi-statement* body — `detail = ...` followed by a
           new multi-line `return {"success": False, ..., "errors":
           captured["errors"], ...}` — that replaces the failure
           return block. The `"errors"` key is only valid because
           633 already added it.

Reconcile correctly kept 633 and excluded 634. But `_diagnose_exclusion`
compared 634's body against `records` (the other file records) instead
of against the cumulative state (`base + 633`), found nothing, and
reported:

  "excluded patch idx=634 ('return {'): introduces an unresolved symbol
   or structural failure; content not found in any other file"

There is no unresolved symbol in 634. `detail`, `captured`, `elapsed`,
and `round` all resolve on `base + 633`. The patch applies cleanly.

The 2026-11-05 fix goes further than the 2026-10-29 one.  Both the
*exclusion search* (`_find_minimum_exclusions` → `_replay_excluding`)
and the *diagnostic* (`_diagnose_exclusion`) now route every patch
application through the single helper `_apply_single_patch_in_replay`,
and the diagnostic path uses a dedicated helper `_replay_state_up_to`
to reconstruct the exact `(content, record_inserted, prior_scopes)`
state that would have been live when the excluded patch was about to
apply.  Without this, the two paths could apply the same patch
against slightly different prior-insertion contexts and produce
different results — the diagnostic would describe a state the
pipeline never reached.

The 2026-10-29 version threaded `prior_insertions` through
`_replay_excluding` but still called `_try_deterministic_patch`
directly from `_diagnose_exclusion` with only `cumulative_base` —
losing the prior-insertion context.  The inpaint_server.py run of
2026-11-05 surfaced that: `_diagnose_exclusion` produced "applies
structurally, but the result fails validate_content: declaration(s)
present in base but missing from result, and still referenced:
['handle_krita_import_masks']", which described neither the state
`_replay_excluding` had built nor the state `_try_deterministic_patch`
actually consumed.

PRIOR-INSERTIONS REPLAY (2026-10-29)
====================================
`_replay_excluding` now threads `prior_insertions` and `prior_scopes`
through `_try_deterministic_patch`, the same way
`pass6_apply_patches` does. The two code paths now see the same
content; without this, reconcile could exclude a patch that
`pass6_apply_patches` had placed correctly using the
prior-insertion fallback, because `_replay_excluding` (which did
not pass `prior_insertions`) failed to reproduce that placement.

CUMULATIVE-BASE COMPUTATION (2026-10-29)
========================================
`_diagnose_exclusion` receives `cumulative_bases[key]` = the state
with all *other* excluded keys applied but not `key` itself. The
prior code computed `excluded_keys - {key}` (which for a
single-exclusion record is the empty set, i.e. "apply everything,
including `key`"). `_try_deterministic_patch(cumulative_base, key)`
then saw `key` as already-applied and returned unchanged — so the
diagnostic always said "applies cleanly" for every single-exclusion
record, regardless of the actual applicability. Fixed to `{key}`.

POST-EXCLUDE STATEMENT AUDIT (2026-10-29, this revision)
=======================================================
`pass8_7_reconcile` now runs `structural_diff(base, new_content,
path, "")` after successful promotion and reports any statement-level
divergence to `rec.warnings`.  Advisory only — does not change trust.

Motivating shape (inpaint_server.py): the excluded patch 634 was a
*modified version of one statement* — the failure return — but
reconcile's only check (`validate_content` + `find_unresolved_
references`) confirms the resulting file parses and resolves.  It
does not notice that a body shaped like a modified return landed as
a duplicate (the excluded patch's contributions arriving through
some other heuristic as dead code).  The statement-level diff names
the divergence: "return {...errors...}" is a signature present in
base? no — present only in the pre-exclusion state; absent from
result.  Combined with the pass-8.10 check on the *unreconciled*
content, this closes the loop: divergence is caught either before
reconcile (reject the patch) or after (warn the user).

The invariant this audit surfaces, without enforcing:
"reconcile promoted, but the promoted content does not carry the
statement shape the excluded patch was trying to introduce, and may
carry a look-alike that parses."  That sentence, as a warning, is
what a user reading `warnings.txt` actually needs.

Prior docstring content (unchanged):
- STALE-WARNING CLEANUP
- THREE MORE CLASSES OF STALE WARNING
- PREFIX-MATCHING CORRECTION (2026-10-28e)
- DIAGNOSTIC MESSAGE SPLIT (2026-10-28e)
- ORDERING FIXES
- WARN-ONLY PASSES EXCLUDED FROM FAILURE COUNTING (2026-10-28)
- DISTINCTIVE SCOPE-NAME MISATTRIBUTION (2026-10-28b)
- MISATTRIBUTION GUARD (raised thresholds)
"""

import re
import sys
from dataclasses import replace
from typing import Optional

from .apply_patches import (
    _apply_pairs, _broader_context_for, _try_deterministic_patch,
)
from .patch_text import _collect_inserted_lines


_NO_OP_STATUSES = frozenset({
    "already-present",
    "skipped_misattributed",
    "skipped_unplaceable",
    "skipped_structural_damage",
})


def _pair_key(pair):
    return ("pair", pair.get("old_idx"), pair.get("new_idx"))


def _patch_key(patch):
    return ("patch", patch.get("idx"))


def _applied_keys(rec):
    keys = []
    for p in rec.patches:
        status = p.get("applied_by")
        if status in _NO_OP_STATUSES:
            continue
        if status == "skipped_weak_attribution":
            continue
        keys.append(_patch_key(p))
    for r in rec.verification.get("pair_records", []):
        status = r.get("applied_by")
        if status in _NO_OP_STATUSES:
            continue
        keys.append(_pair_key(r.get("pair", {})))
    return keys


def _apply_single_patch_in_replay(rec, content, patch, path,
                                  blocks_by_idx, records,
                                  record_inserted, prior_scopes):
    """Apply one patch during replay.  Mutates `record_inserted` in
    place and returns the new content, or None on failure.

    Factored out so `_diagnose_exclusion` and `_replay_excluding`
    execute the exact same patch application against the exact same
    prior-insertions state.  If these diverged, the diagnostic would
    describe a result the pipeline never produced.

    See the module docstring, CUMULATIVE-CONTEXT DIAGNOSTICS for the
    motivating failure (`inpaint_server.py` in the 2026-11-05 run).
    """
    result = _try_deterministic_patch(
        content, patch, path,
        prior_insertions=record_inserted,
        prior_scopes=prior_scopes,
    )
    if result is None:
        broader = _broader_context_for(
            patch, blocks_by_idx, records, path)
        if broader != patch.get("context_before", ""):
            broader_patch = {**patch, "context_before": broader}
            result = _try_deterministic_patch(
                content, broader_patch, path,
                prior_insertions=record_inserted,
                prior_scopes=prior_scopes,
            )
    if result is None:
        return None
    if result != content:
        record_inserted |= _collect_inserted_lines(content, result)
    return result


def _replay_excluding(rec, base, excluded_keys, path,
                      blocks_by_idx, records,
                      ignore_status=False):
    """Replay pass-6 application with `excluded_keys` removed.

    `path` is passed to `_apply_pairs` so scope replaces executed
    during replay get AST-snapped boundaries (the same treatment as
    the initial application).

    `prior_insertions` / `prior_scopes` are tracked the same way
    `pass6_apply_patches` does. Without them, replay could not
    reproduce the pass-6 result and reconcile's exclusion search
    would fail for every record whose patches depend on the
    prior-insertion fallback. See the module docstring,
    PRIOR-INSERTIONS REPLAY.

    `ignore_status=False` (the default) preserves the original
    semantics: patches whose `applied_by` is in `_NO_OP_STATUSES` or
    is `"failed"` are skipped. `ignore_status=True` re-applies every
    patch not in `excluded_keys`, regardless of its recorded status;
    used by the diagnostic path after statuses have been mutated.

    All patch applications are routed through
    `_apply_single_patch_in_replay` so this function and
    `_diagnose_exclusion` cannot drift apart.
    """
    content = base

    record_inserted: set = set()
    prior_scopes: dict = {}

    pairs_kept = [
        pair for pair in rec.pairs
        if _pair_key(pair) not in excluded_keys
    ]
    if pairs_kept:
        new_content, _ = _apply_pairs(content, pairs_kept, path=path)
        record_inserted |= _collect_inserted_lines(content, new_content)
        content = new_content

    for patch in rec.patches:
        if _patch_key(patch) in excluded_keys:
            continue
        status = patch.get("applied_by")
        if not ignore_status and (
                status in _NO_OP_STATUSES or status == "failed"):
            continue

        new_content = _apply_single_patch_in_replay(
            rec, content, patch, path,
            blocks_by_idx, records,
            record_inserted, prior_scopes,
        )
        if new_content is None:
            return None
        content = new_content

    return content


def _replay_state_up_to(rec, base, excluded_keys, stop_key, path,
                        blocks_by_idx, records,
                        ignore_status=False):
    """Replay up to (but not including) `stop_key`.

    Returns `(content, record_inserted, prior_scopes)` — the same
    state `_apply_single_patch_in_replay` mutates.  The diagnostic
    path uses this to apply `stop_key` against the exact cumulative
    state that would have been live when the patch was originally
    attempted.  See the module docstring, CUMULATIVE-CONTEXT
    DIAGNOSTICS.

    Returns None when a prerequisite patch cannot be replayed (the
    diagnostic then falls back to `cumulative_base`-only comparison).
    """
    content = base
    record_inserted: set = set()
    prior_scopes: dict = {}

    pairs_kept = [
        pair for pair in rec.pairs
        if _pair_key(pair) not in excluded_keys
    ]
    if pairs_kept:
        new_content, _ = _apply_pairs(content, pairs_kept, path=path)
        record_inserted |= _collect_inserted_lines(content, new_content)
        content = new_content

    for patch in rec.patches:
        if _patch_key(patch) == stop_key:
            break
        if _patch_key(patch) in excluded_keys:
            continue
        status = patch.get("applied_by")
        if not ignore_status and (
                status in _NO_OP_STATUSES or status == "failed"):
            continue
        new_content = _apply_single_patch_in_replay(
            rec, content, patch, path,
            blocks_by_idx, records,
            record_inserted, prior_scopes,
        )
        if new_content is None:
            return None
        content = new_content

    return (content, record_inserted, prior_scopes)


def _count_failures(content, path, rec, base, records, excluded_keys):
    """Return (n_failures, [failure_descriptors]).

    Runs every *demoting* check pass 8.x performs. Lint and
    added-param-lint are NOT included: both are warn-only (see
    `lint.py` and `verify_applied.py` docstrings).
    """
    from .validate_output import validate_content
    from .symbols import find_unresolved_references

    failures = []

    err = validate_content(content, path, base=base)
    if err is not None:
        failures.append(("content_invalid", err))

    trial = replace(rec, content=content)
    unresolved, mismatched = find_unresolved_references(
        trial, base, records)
    if unresolved or mismatched:
        failures.append(("unresolved_calls", {
            "unresolved": unresolved,
            "mismatched": mismatched,
        }))

    for p in rec.patches:
        if p.get("applied_by") != "failed":
            continue
        if _patch_key(p) in excluded_keys:
            continue
        failures.append(("patch_failed", p.get("idx")))

    for r in rec.verification.get("pair_records", []):
        if r.get("applied_by") != "failed":
            continue
        pk = _pair_key(r.get("pair", {}))
        if pk in excluded_keys:
            continue
        failures.append(("pair_failed", pk))

    return len(failures), failures


def _find_minimum_exclusions(rec, base, records, path, blocks_by_idx):
    candidates = _applied_keys(rec)
    if not candidates:
        return None

    excluded = set()
    for _ in range(len(candidates) + 1):
        content = _replay_excluding(
            rec, base, excluded, path, blocks_by_idx, records)
        if content is None:
            return None
        failures, _ = _count_failures(
            content, path, rec, base, records, excluded)
        if failures == 0:
            return sorted(excluded)

        best_gain = None
        for key in candidates:
            if key in excluded:
                continue
            trial_set = excluded | {key}
            trial_content = _replay_excluding(
                rec, base, trial_set, path, blocks_by_idx, records)
            if trial_content is None:
                continue
            trial_failures, _ = _count_failures(
                trial_content, path, rec, base, records, trial_set)
            gain = failures - trial_failures
            if gain <= 0:
                continue
            if best_gain is None or gain > best_gain[0]:
                best_gain = (gain, key)
        if best_gain is None:
            return None
        excluded.add(best_gain[1])

    return None


def _is_distinctive_scope_name(name):
    if not name or len(name) < 8:
        return False
    return "_" in name or any(c.isupper() for c in name)


def _scope_name_lives_elsewhere(body, current_path, records):
    from .prose import _extract_scope_from_body
    from .patch_text import _content_present_in_base
    from .dedup import _top_level_decls

    scope_name, _ = _extract_scope_from_body(body)
    if not _is_distinctive_scope_name(scope_name):
        return None

    if len(_top_level_decls(body)) != 1:
        return None

    decl_pat = re.compile(
        r'(?:function|def|class|const|let|var)\s+'
        + re.escape(scope_name) + r'\b'
        r'|<summary>\s*' + re.escape(scope_name) + r'\s*</summary>')

    for other_path, other_rec in records.items():
        if other_path == current_path:
            continue
        if other_rec.content is None:
            continue
        if not decl_pat.search(other_rec.content):
            continue
        if _content_present_in_base(other_rec.content, body,
                                     threshold=0.4):
            return other_path
    return None


def _fuzzy_locate_elsewhere(body, current_path, records):
    """If `body` matches another record's content, return that
    record's path; else None.
    """
    from .patch_text import _content_present_in_base
    body = (body or "").strip()
    if not body or len(body) < 40:
        return None

    scope_match = _scope_name_lives_elsewhere(body, current_path, records)
    if scope_match is not None:
        return scope_match

    if len(body) < 500:
        best = None
        for other_path, other_rec in records.items():
            if other_path == current_path:
                continue
            if other_rec.content is None:
                continue
            if body in other_rec.content:
                if best is None or len(other_rec.content) < best[1]:
                    best = (other_path, len(other_rec.content))
        return best[0] if best else None

    best = None
    for other_path, other_rec in records.items():
        if other_path == current_path:
            continue
        if other_rec.content is None:
            continue
        if _content_present_in_base(other_rec.content, body,
                                     threshold=0.85):
            if best is None or len(other_rec.content) < best[1]:
                best = (other_path, len(other_rec.content))
    return best[0] if best else None


def _patch_first_line(patch):
    body = patch.get("body", "") or ""
    for ln in body.splitlines():
        s = ln.strip()
        if s:
            return s[:70]
    return ""


def _diagnose_exclusion(key, rec, records, original_status=None,
                        cumulative_base=None, cumulative_state=None):
    """Produce a human-readable exclusion message.

    Five distinct cases:

      1. `original_status == "failed"` — never applied.  "Could not
         be placed."

      2. `cumulative_state` is provided (the 2026-11-05 fix): a
         `(content, record_inserted, prior_scopes)` tuple
         reconstructed by `_replay_state_up_to`.  The excluded patch
         is applied against that exact state — same prior-insertions,
         same broader-context retry, same result as the pipeline
         would have produced — and `validate_content` +
         `find_unresolved_references` describe the outcome.
         See the module docstring, CUMULATIVE-CONTEXT DIAGNOSTICS.

      3. `cumulative_base` is provided but `cumulative_state` is not:
         fall back to the 2026-10-29 behaviour, which applies the
         patch against `cumulative_base` without prior-insertions.
         This is what the diagnostic used to do; retained for callers
         that only have a content-only base.

      4. `_fuzzy_locate_elsewhere` finds a match — applied but
         belongs elsewhere.  "Content already exists in X;
         misattributed."

      5. Neither — applied but produces a validation failure that
         couldn't be attributed.  "Introduces X; content not found
         elsewhere."

    Cases 2 and 3 both extend the pre-fix diagnostics.  They differ in
    what state they apply the patch to; 2 is what the pipeline
    actually saw, 3 is a best-effort reconstruction when the pipeline
    state is unavailable.
    """
    if key[0] == "patch":
        idx = key[1]
        patch = next(
            (p for p in rec.patches if p.get("idx") == idx), None)
        if patch is None:
            return f"excluded patch idx={idx}"
        status = (original_status if original_status is not None
                  else patch.get("applied_by"))
        first = _patch_first_line(patch)
        body = patch.get("body", "") or ""

        # Case 2: cumulative-state diagnostic.  Uses the exact
        # prior_insertions/prior_scopes context the pipeline built.
        if cumulative_state is not None and body.strip():
            content, record_inserted, prior_scopes = cumulative_state
            try:
                result = _apply_single_patch_in_replay(
                    rec, content, patch, rec.path,
                    {}, {}, record_inserted, prior_scopes,
                )
            except Exception:
                result = None
            if result is not None:
                from .validate_output import validate_content
                from .symbols import find_unresolved_references
                err = validate_content(
                    result, rec.path, base=content)
                if err is not None:
                    return (
                        f"excluded patch idx={idx} ({first!r}): "
                        f"applies structurally, but the result "
                        f"fails validate_content: {err}"
                    )
                try:
                    trial = replace(rec, content=result)
                    unresolved, _ = find_unresolved_references(
                        trial, content, records)
                except Exception:
                    unresolved = []
                if not unresolved:
                    return (
                        f"excluded patch idx={idx} ({first!r}): "
                        f"applies on cumulative state and produces "
                        f"valid content with no unresolved symbols; "
                        f"reconcile excluded it for the wrong "
                        f"reason \u2014 please report"
                    )
                return (
                    f"excluded patch idx={idx} ({first!r}): "
                    f"applies structurally, but introduces "
                    f"unresolved symbol(s) {unresolved} on "
                    f"cumulative state"
                )
            # result is None: the patch doesn't apply on cumulative
            # state.  Diagnose the specific failure reason.
            try:
                from .patch_diag import _diagnose_patch_failure
                underlying = _diagnose_patch_failure(
                    patch, content, content)
            except Exception:
                underlying = None
            if underlying:
                return (
                    f"excluded patch idx={idx} ({first!r}): "
                    f"fails on cumulative state: {underlying}"
                )

        # Case 3: content-only cumulative diagnostic (2026-10-29
        # behaviour; kept for callers that only have the base).
        elif cumulative_base is not None and body.strip():
            try:
                result = _try_deterministic_patch(
                    cumulative_base, patch, rec.path)
            except Exception:
                result = None
            if result is not None:
                from .validate_output import validate_content
                from .symbols import find_unresolved_references
                err = validate_content(
                    result, rec.path, base=cumulative_base)
                if err is not None:
                    return (
                        f"excluded patch idx={idx} ({first!r}): "
                        f"applies structurally, but the result "
                        f"fails validate_content: {err}"
                    )
                try:
                    trial = replace(rec, content=result)
                    unresolved, _ = find_unresolved_references(
                        trial, cumulative_base, records)
                except Exception:
                    unresolved = []
                if not unresolved:
                    return (
                        f"excluded patch idx={idx} ({first!r}): "
                        f"applies on cumulative state and produces "
                        f"valid content with no unresolved symbols; "
                        f"reconcile excluded it for the wrong "
                        f"reason \u2014 please report"
                    )
                return (
                    f"excluded patch idx={idx} ({first!r}): "
                    f"applies structurally, but introduces "
                    f"unresolved symbol(s) {unresolved} on "
                    f"cumulative state"
                )
            try:
                from .patch_diag import _diagnose_patch_failure
                underlying = _diagnose_patch_failure(
                    patch, cumulative_base, cumulative_base)
            except Exception:
                underlying = None
            if underlying:
                return (
                    f"excluded patch idx={idx} ({first!r}): "
                    f"fails on cumulative state: {underlying}"
                )

        # Pre-fix diagnostics.
        if status == "failed":
            return (f"excluded patch idx={idx} ({first!r}): "
                    f"could not be placed; the rest of the record "
                    f"applies cleanly without it")
        other = _fuzzy_locate_elsewhere(
            body, rec.path, records)
        if other is not None:
            return (f"excluded patch idx={idx} ({first!r}): "
                    f"content already exists in {other} — "
                    f"likely misattributed, not written here")
        return (f"excluded patch idx={idx} ({first!r}): "
                f"introduces an unresolved symbol or structural "
                f"failure; content not found in any other file")
    oi, ni = key[1], key[2]
    return (f"excluded pair (old={oi}, new={ni}): "
            f"could not be reconciled; the rest of the record "
            f"applies cleanly without it")


def _trust_cap(rec):
    if rec.verification.get("reconciled"):
        return "MEDIUM"
    return "MEDIUM" if rec.dedup_reason == "last_full" else "HIGH"


def _base_content(rec, blocks_by_idx):
    if blocks_by_idx is None:
        return None
    if rec.last_full_idx is None:
        return None
    block = blocks_by_idx.get(rec.last_full_idx)
    if block is None:
        return None
    return block.body


def _clear_stale_pass6_warnings(rec, excluded_keys):
    excluded_patch_idxs = set()
    for key in excluded_keys:
        if key[0] == "patch":
            excluded_patch_idxs.add(key[1])

    filtered = []
    for w in rec.warnings:
        if w.startswith("patch idx="):
            try:
                after = w[len("patch idx="):]
                num_str = after.split(" ", 1)[0]
                idx = int(num_str)
            except (ValueError, IndexError):
                filtered.append(w)
                continue
            if idx in excluded_patch_idxs:
                continue
            filtered.append(w)
            continue

        if "replace pair(s) failed" in w:
            live_failed = sum(
                1 for r in rec.verification.get("pair_records", [])
                if r.get("applied_by") == "failed"
            )
            if live_failed == 0:
                continue
            filtered.append(w)
            continue

        if (w.startswith(
                "patch compatibility check failed:")
                or w.startswith(
                    "unresolved symbol reference(s) introduced by patch:")):
            if (not rec.verification.get("unresolved_calls")
                    and not rec.verification.get("signature_mismatches")):
                continue
            filtered.append(w)
            continue

        if w.startswith("signature mismatch(es):"):
            if not rec.verification.get("signature_mismatches"):
                continue
            filtered.append(w)
            continue

        if w.startswith("content validation failed:"):
            if rec.verification.get("content_valid") == "ok":
                continue
            filtered.append(w)
            continue

        filtered.append(w)
    rec.warnings = filtered


def pass8_7_reconcile(records, blocks_by_idx, verbose=False):
    """Reconcile fail-closed records by excluding the minimum set of
    patches/pairs that caused the failure."""
    n_reconciled = 0

    for path, rec in records.items():
        if rec.trust != "LOW":
            continue
        if rec.content is None:
            continue
        if rec.is_deleted:
            continue
        if not (rec.patches or rec.pairs):
            continue

        if rec.verification.get("partial_reprint"):
            if verbose:
                print(f"  [reconcile] {path}: skipping — partial "
                      f"reprint", file=sys.stderr)
            continue

        base = _base_content(rec, blocks_by_idx)
        if base is None:
            continue

        excluded_keys = _find_minimum_exclusions(
            rec, base, records, path, blocks_by_idx)
        if excluded_keys is None:
            if verbose:
                print(f"  [reconcile] {path}: no valid exclusion "
                      f"set found", file=sys.stderr)
            continue

        new_content = _replay_excluding(
            rec, base, set(excluded_keys), path,
            blocks_by_idx, records)
        if new_content is None:
            continue

        if new_content == base:
            if verbose:
                print(f"  [reconcile] {path}: not promoting — "
                      f"reconciliation to base is a no-op",
                      file=sys.stderr)
            continue

        original_statuses = {}
        for key in excluded_keys:
            if key[0] == "patch":
                idx = key[1]
                patch = next(
                    (p for p in rec.patches if p.get("idx") == idx),
                    None)
                if patch is None:
                    continue
                original_statuses[idx] = patch.get("applied_by")
                if patch.get("applied_by") == "failed":
                    patch["applied_by"] = "skipped_unplaceable"
                else:
                    patch["applied_by"] = "skipped_misattributed"
            else:
                oi, ni = key[1], key[2]
                for r in rec.verification.get("pair_records", []):
                    pair = r.get("pair", {})
                    if (pair.get("old_idx") == oi
                            and pair.get("new_idx") == ni):
                        if r.get("applied_by") == "failed":
                            r["applied_by"] = "skipped_unplaceable"
                        else:
                            r["applied_by"] = "skipped_misattributed"
                        break

        from .validate_output import validate_content
        err = validate_content(new_content, path, base=base)
        if err is not None:
            continue
        from .symbols import find_unresolved_references
        trial = replace(rec, content=new_content)
        unresolved, mismatched = find_unresolved_references(
            trial, base, records)
        if unresolved or mismatched:
            continue

        # NEW (2026-10-29): capture pre-reconcile content for
        # the post-exclude statement audit below.  The audit's
        # intent is to name what reconcile *changed by
        # excluding* — which requires comparing the
        # pre-exclusion state against the promoted state, not
        # the original base against the promoted state.  The
        # difference between base and post-promote includes
        # every applied patch's contribution, which drowns the
        # excluded items in noise.
        _pre_reconcile_content = rec.content
        rec.content = new_content

        rec.verification["patches_applied"] = sum(
            1 for p in rec.patches
            if p.get("applied_by")
            not in ("failed", "skipped_weak_attribution",
                    "skipped_misattributed",
                    "skipped_unplaceable"))
        rec.verification["pairs_applied"] = sum(
            1 for r in rec.verification.get("pair_records", [])
            if r.get("applied_by") == "deterministic")
        rec.verification.pop("patches_incomplete", None)
        rec.verification.pop("pairs_incomplete", None)
        rec.verification.pop("unresolved_calls", None)
        rec.verification.pop("signature_mismatches", None)
        rec.verification["content_valid"] = "ok"

        if path.endswith(".py"):
            import ast
            try:
                ast.parse(new_content)
                rec.verification["ast_parse"] = "ok"
            except SyntaxError:
                continue

        rec.sanity_rejected = False
        rec.verification.pop("sanity_reason", None)
        rec.verification["reconciled"] = {
            "excluded": [
                {"kind": k[0], **(
                    {"idx": k[1]} if k[0] == "patch"
                    else {"old_idx": k[1], "new_idx": k[2]}
                )} for k in excluded_keys
            ],
        }
        rec.trust = _trust_cap(rec)

        _clear_stale_pass6_warnings(rec, excluded_keys)

        # Cumulative states: for each excluded key, the *exact*
        # `(content, record_inserted, prior_scopes)` state that would
        # have been live when the excluded patch was about to apply —
        # i.e. all OTHER patches applied (including any excluded
        # patches OTHER than this key), and this key itself NOT
        # applied.  Then the diagnostic replays `key` against that
        # state using the same `_apply_single_patch_in_replay` helper
        # the exclusion search uses.
        #
        # The 2026-11-05 refinement: `_replay_state_up_to` returns
        # the full tuple, not just the content.  Without it, the
        # diagnostic applied the patch against a state that had no
        # `prior_insertions` context, and produced a message describing
        # a result the pipeline never reached.
        cumulative_states = {}
        for key in excluded_keys:
            cumulative_states[key] = _replay_state_up_to(
                rec, base, set(excluded_keys) - {key}, key, path,
                blocks_by_idx, records,
                ignore_status=True,
            )
        cumulative_bases = {
            k: v[0] if v is not None else None
            for k, v in cumulative_states.items()
        }

        for key in excluded_keys:
            original_status = None
            if key[0] == "patch":
                original_status = original_statuses.get(key[1])
            diag = _diagnose_exclusion(
                key, rec, records,
                original_status=original_status,
                cumulative_base=cumulative_bases.get(key),
                cumulative_state=cumulative_states.get(key),
            )
            rec.warnings.append(diag)
            if verbose:
                print(f"  [reconcile] {path}: {diag}", file=sys.stderr)

        # NEW (2026-10-29): post-exclude statement-level audit.
        # Reconcile's job is to *promote* content that parses and
        # resolves.  It does not — cannot, given its inputs — notice
        # that the promotion leaves behind a shape the excluded patch
        # was trying to eliminate.  The concrete case: a body shaped
        # like `return {"success": False, ..., "errors": ...}`
        # (the excluded patch) was applied by the pipeline through an
        # alternative heuristic as a *duplicate* return; parse passes,
        # no dangling symbol, but the file has a dead statement.  A
        # statement-level `structural_diff` names it.  Advisory only
        # — the promoted content is still what gets written, and the
        # user is told what shape drift to look for in the diff.
        try:
            from .structural_diff import structural_diff
            stmt_divergence = structural_diff(
                _pre_reconcile_content, new_content, path, "")
            if stmt_divergence:
                rec.warnings.append(
                    "reconcile: statement-level divergence after "
                    "exclusion — review the residual for shape drift: "
                    + "; ".join(
                        f"{c.kind} {c.signature!r}"
                        for c in stmt_divergence[:5]
                    )
                )
                if verbose:
                    print(
                        f"  [reconcile] {path}: statement-level "
                        f"divergence after exclusion "
                        f"({len(stmt_divergence)} change(s))",
                        file=sys.stderr,
                    )
        except Exception as _e:
            # The audit is advisory; a raise here must not undo a
            # successful promotion.
            rec.warnings.append(
                f"reconcile: statement-level audit raised "
                f"{type(_e).__name__}: {_e}"
            )

        n_reconciled += 1
        if verbose:
            print(f"  [reconcile] {path}: reconciled "
                  f"({len(excluded_keys)} exclusion(s); "
                  f"trust={rec.trust})", file=sys.stderr)

    return n_reconciled
