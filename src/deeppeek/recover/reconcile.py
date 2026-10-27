"""
recover.reconcile — Pass 8.7: reconcile fail-closed records by
excluding the minimum set of patches/pairs that caused the failure.

See the pre-fix module docstring for the motivating cases and the
"reconciled to base is not a rescue" invariant.

NEW: STALE-WARNING CLEANUP
==========================
Pass 6 appends `patch idx={idx} could not be applied (...)` warnings
for every failed patch.  When reconciliation excludes a patch and
promotes the record, those warnings are now stale — the patch is
`skipped_unplaceable` or `skipped_misattributed`, not "failed".
The pre-fix version left the warnings in place, producing
confusing `warnings.txt` output like:

    [pg_panel_bindings.py] [WARN] patch idx=90 could not be applied (...)
    [pg_panel_bindings.py] [WARN] excluded patch idx=90 (...): ...

on a record that was written at HIGH trust.  `pass8_7_reconcile`
now removes the pass-6 warning for each excluded patch idx before
adding its own reconciliation warning.

The `patch or pair application failed; file not written` warning
from pass 6 is also removed by `pass6_apply_patches` itself (see
that module) — it never existed to be cleaned up here.  The
"file not written" state is now reported accurately by
`pass9_write`'s `trust=LOW below --min-trust` line, which fires
only for records that actually stay LOW.
"""

import sys
from dataclasses import replace
from typing import Optional

from .apply_patches import (
    _apply_pairs, _broader_context_for, _try_deterministic_patch,
)


_NO_OP_STATUSES = frozenset({
    "already-present",
    "skipped_misattributed",
    "skipped_unplaceable",
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


def _replay_excluding(rec, base, excluded_keys, path,
                      blocks_by_idx, records):
    content = base

    pairs_kept = [
        pair for pair in rec.pairs
        if _pair_key(pair) not in excluded_keys
    ]
    if pairs_kept:
        content, _ = _apply_pairs(content, pairs_kept)

    for patch in rec.patches:
        if _patch_key(patch) in excluded_keys:
            continue
        status = patch.get("applied_by")
        if status in _NO_OP_STATUSES or status == "failed":
            continue

        result = _try_deterministic_patch(content, patch, path)
        if result is None:
            broader = _broader_context_for(
                patch, blocks_by_idx, records, path)
            if broader != patch.get("context_before", ""):
                broader_patch = {
                    **patch, "context_before": broader}
                result = _try_deterministic_patch(
                    content, broader_patch, path)
        if result is None:
            return None
        content = result

    return content


def _count_failures(content, path, rec, base, records, excluded_keys):
    from .validate_output import validate_content
    from .symbols import find_unresolved_calls

    failures = []

    err = validate_content(content, path, base=base)
    if err is not None:
        failures.append(("content_invalid", err))

    trial = replace(rec, content=content)
    unresolved = find_unresolved_calls(trial, base, records)
    if unresolved:
        failures.append(("unresolved_calls", list(unresolved)))

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


def _fuzzy_locate_elsewhere(body, current_path, records):
    from .patch_text import _content_present_in_base
    body = (body or "").strip()
    if not body or len(body) < 40:
        return None
    best = None
    for other_path, other_rec in records.items():
        if other_path == current_path:
            continue
        if other_rec.content is None:
            continue
        if _content_present_in_base(other_rec.content, body,
                                     threshold=0.7):
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


def _diagnose_exclusion(key, rec, records):
    if key[0] == "patch":
        idx = key[1]
        patch = next(
            (p for p in rec.patches if p.get("idx") == idx), None)
        if patch is None:
            return f"excluded patch idx={idx}"
        status = patch.get("applied_by")
        first = _patch_first_line(patch)
        if status == "failed":
            return (f"excluded patch idx={idx} ({first!r}): "
                    f"could not be placed; the rest of the record "
                    f"applies cleanly without it")
        other = _fuzzy_locate_elsewhere(
            patch.get("body", ""), rec.path, records)
        if other is not None:
            return (f"excluded patch idx={idx} ({first!r}): "
                    f"introduces an unresolved symbol or structural "
                    f"failure; content matches {other} — likely "
                    f"misattributed, not written here")
        return (f"excluded patch idx={idx} ({first!r}): "
                f"introduces an unresolved symbol or structural "
                f"failure; content not found in any other file")
    oi, ni = key[1], key[2]
    return (f"excluded pair (old={oi}, new={ni}): "
            f"could not be reconciled; the rest of the record "
            f"applies cleanly without it")


def _trust_cap(rec):
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


# NEW: warning patterns emitted by pass 6 that reconcile should
# remove when it excludes the corresponding patch.  The
# `patch idx=N could not be applied` line is per-patch; reconcile
# removes it for each excluded idx.  The
# `patch or pair application failed; file not written` line was
# removed from pass 6 entirely (see apply_patches.py) so there is
# nothing to clean up for it here.
_PASS6_PATCH_WARN_PREFIX = "patch idx="


def _clear_stale_pass6_warnings(rec, excluded_keys):
    """Remove warnings that are stale after reconciliation.

    Three warning classes are cleaned up:

    1. `patch idx=N could not be applied (...)` — emitted by pass 6
       for each failed patch.  When reconcile excludes patch N, the
       patch is `skipped_unplaceable`/`skipped_misattributed`, not
       "failed".  The per-patch warning is stale.

    2. `N/M replace pair(s) failed: ...` — emitted by pass 6 when one
       or more pairs fail.  After reconcile excludes the failing
       pair(s), the record's live `pair_records` may have no
       `applied_by == "failed"` entries.  The aggregate warning is
       stale.

    3. `unresolved symbol reference(s) introduced by patch: ...` —
       emitted by pass 8.5 for records whose patches introduce
       unresolved calls.  Reconcile pop's `rec.verification[
       "unresolved_calls"]` after re-verifying the record is clean.
       The pass-8.5 warning is stale.

    Idempotent: calling twice does nothing the second time.
    """
    excluded_patch_idxs = set()
    for key in excluded_keys:
        if key[0] == "patch":
            excluded_patch_idxs.add(key[1])

    filtered = []
    for w in rec.warnings:
        # Class 1: per-patch "could not be applied" warnings.
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

        # Class 2: pair-failure aggregate warning.  Live state check:
        # after reconcile has run, `pair_records` no longer contains
        # any `failed` entries (reconcile rewrote them to
        # `skipped_unplaceable` or `skipped_misattributed`).
        if "replace pair(s) failed" in w:
            live_failed = sum(
                1 for r in rec.verification.get("pair_records", [])
                if r.get("applied_by") == "failed"
            )
            if live_failed == 0:
                continue
            filtered.append(w)
            continue

        # Class 3: unresolved-symbol warning from pass 8.5.  Live
        # state check: reconcile pop's `unresolved_calls` before
        # calling this helper, so the field is either absent or
        # empty when we're promoting.
        if w.startswith(
                "unresolved symbol reference(s) introduced by patch:"):
            if not rec.verification.get("unresolved_calls"):
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
        if getattr(rec, "sanity_rejected", False):
            continue
        if not (rec.patches or rec.pairs):
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

        for key in excluded_keys:
            if key[0] == "patch":
                idx = key[1]
                patch = next(
                    (p for p in rec.patches if p.get("idx") == idx),
                    None)
                if patch is None:
                    continue
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
        from .symbols import find_unresolved_calls
        trial = replace(rec, content=new_content)
        if find_unresolved_calls(trial, base, records):
            continue

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
        rec.verification["content_valid"] = "ok"

        if path.endswith(".py"):
            import ast
            try:
                ast.parse(new_content)
                rec.verification["ast_parse"] = "ok"
            except SyntaxError:
                continue

        rec.trust = _trust_cap(rec)
        rec.verification["reconciled"] = {
            "excluded": [
                {"kind": k[0], **(
                    {"idx": k[1]} if k[0] == "patch"
                    else {"old_idx": k[1], "new_idx": k[2]}
                )} for k in excluded_keys
            ],
        }

        # NEW: clear pass-6 stale warnings for excluded patches
        # before adding the reconcile-specific warnings.
        _clear_stale_pass6_warnings(rec, excluded_keys)

        for key in excluded_keys:
            diag = _diagnose_exclusion(key, rec, records)
            rec.warnings.append(diag)
            if verbose:
                print(f"  [reconcile] {path}: {diag}", file=sys.stderr)

        n_reconciled += 1
        if verbose:
            print(f"  [reconcile] {path}: reconciled "
                  f"({len(excluded_keys)} exclusion(s); "
                  f"trust={rec.trust})", file=sys.stderr)

    return n_reconciled
