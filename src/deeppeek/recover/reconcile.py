"""
recover.reconcile — Pass 8.7: reconcile fail-closed records by
excluding the minimum set of patches/pairs that caused the failure.

The pass-8.5 symbol check and the structural sanity gate both detect
*real* problems — but they fail-close the *record*, which throws away
the parts of the record that were fine.  A record can contain:

  · one misattributed patch (content from another file, or a stray
    thought fragment) alongside three correct patches;
  · one patch whose body omits its closing brace alongside two
    patches that apply cleanly;
  · one applied patch that introduces an unresolved symbol alongside
    the ones that don't.

In all three cases the *record* fails, but the *content* is mostly
correct.  Writing only the correct parts — and telling the user
exactly which patch was excluded and why — is strictly better than
fail-closing the whole file.

Reconciliation does this generally, without knowing *why* a patch
is bad:

  1. For each record that would fail-close, try replaying with each
     candidate patch/pair excluded in turn.
  2. Keep the exclusion that most reduces the failure count.  Repeat
     until the record passes or no exclusion helps.
  3. If it passes, commit: mark each excluded item
     `skipped_misattributed` (was applied) or `skipped_unplaceable`
     (was failed), rebuild content, restore trust to the record cap.
  4. Emit one warning per excluded item, naming what failed and
     — when the excluded content matches a different record — where
     it probably belonged.

"RECONCILED TO BASE" IS NOT A RESCUE
====================================
A record whose ONLY content is the failing item — one patch that
didn't apply, one pair that failed, one fragment whose symbol is
unresolved — can be trivially "fixed" by excluding that item.  The
resulting content is byte-identical to `base`.  That is not a rescue:
no change is being written.  The user asked for a specific edit and
the edit could not be placed.

`pass8_7_reconcile` refuses to promote in that case.  The record
stays fail-closed, its diagnostic stays in `warnings.txt`, and the
target file is preserved.

REPLAY MUST MIRROR PASS 6's RETRY SEQUENCE
==========================================
Pass 6 tries each patch with the patch's own `context_before`
first, then — if that fails — retries with `_broader_context_for`
(merged context from adjacent blocks in the same record).  Some
patches only apply on the second attempt: block 97's rotation-field
patch in `pg_panel_scrubs.py`, for example, has only "Then append:"
as its own context but succeeds because block 96's context (the
import update, which sits immediately above it) reads
"Add at the bottom (before the `status.running` field).  Also
update the import line:".  The merged context contains the anchor
phrase "before `status.running`", so `_try_insert_before_anchor`
fires.

`_replay_excluding` must therefore use the SAME retry sequence.  If
it only tries the original `context_before`, replay fails for any
patch that was originally applied via broader context — and
reconciliation bails with `None`, leaving the fail-closed outcome
in place.  That was the specific reason `pg_panel_scrubs.py` stayed
LOW after pass 8.7 was first added: block 97 could not be replayed
without the merged context, so excluding block 86 was never even
tried.

Interaction with pass 8.5 (symbols): the symbol check records an
unresolved call on the *record*, not the patch.  Reconciliation
rebuilds the record's content from `base` + kept items, so pass
8.5's finding is re-evaluated against the rebuilt content — if the
excluded patch was the source of the unresolved call, the rebuilt
content has no unresolved calls and the record promotes.

Interaction with pass 7.6/7.7 (JS export/import): those run *before*
reconcile, so if repair succeeded the record never reached LOW.
Reconcile is the fallback when repair couldn't fix the finding.

Interaction with the sanity gate: a sanity_rejected record has
already had its content reverted to base by the sanity gate.
Reconciliation does not touch those (they are structurally defective
end to end).
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
    """Return the patch/pair keys worth trying to exclude.

    Includes applied patches (deterministic, llm), applied pairs, and
    failed patches/pairs.  Failed items are candidates because
    excluding them removes a `patch_failed` / `pair_failed` failure
    from the record's accounting without changing content.
    """
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
    """Replay the record's pairs then patches against `base`, skipping
    anything whose key is in `excluded_keys` AND skipping failed
    items (they contributed nothing to content in the original run).
    Returns the resulting content string, or None if a kept applied
    patch could not be applied.

    Uses the same retry sequence pass6 uses (see "REPLAY MUST MIRROR
    PASS 6's RETRY SEQUENCE" in the module docstring): each patch is
    tried with its original `context_before`, then with
    `_broader_context_for`'s merged context.  Without this, any patch
    that was originally applied only via broader-context retry would
    fail in the replay — even though the exact same patch applied
    cleanly in the original pass — and reconciliation would bail
    with `None` (treating the record as unrecoverable).
    """
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
    """Return (count, list-of-descriptions) of validation failures
    against `content`, honoring `excluded_keys` for the failed-patch
    and failed-pair accounting."""
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
    """Return the minimal set of exclusion keys that yields zero
    failures, or None if no exclusion set achieves that.

    Greedy iterative: at each step, try each candidate exclusion and
    pick the one that most reduces the failure count.  Stops when the
    record passes, when no exclusion helps, or when every candidate
    has been tried.
    """
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
    """Search other records' content for a fuzzy match to `body`.
    Returns the best-matching path, or None."""
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


def pass8_7_reconcile(records, blocks_by_idx, verbose=False):
    """Reconcile fail-closed records by excluding the minimum set of
    patches/pairs that caused the failure.

    Mutates records in place.  Records that cannot be reconciled (no
    exclusion set yields a valid record) are left untouched (their
    trust stays LOW).  Records whose reconciliation produces content
    identical to base are also left untouched — see "reconciled to
    base is not a rescue" in the module docstring.

    Returns the number of records actually promoted (trust restored).
    """
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

        # Rebuild content FIRST so the to-base guard below can inspect
        # it without committing the exclusion set.
        new_content = _replay_excluding(
            rec, base, set(excluded_keys), path,
            blocks_by_idx, records)
        if new_content is None:
            continue

        # Reconciliation to base is not a rescue.  A record whose
        # only applied items get excluded has no changes to write;
        # promoting it would hide the fact that the user's
        # requested edits never landed.  Fail-closed is the honest
        # response — the diagnostic (which patch/pair failed and
        # why) is the useful output, not a green checkmark.
        #
        # The five fail-closed test fixtures in test_recover.py
        # (11, 16, 17, 26, 32) all land in this branch: a single
        # patch or pair that failed or introduced an unresolved
        # call, no other content to preserve.  They stay LOW.
        #
        # The case reconciliation IS for — the inpaint
        # conversation's pg_panel_scrubs.py, where two clean
        # patches (import + rotation block) would land once a
        # misattributed fragment is excluded — produces
        # new_content != base and falls through to promote.
        if new_content == base:
            if verbose:
                print(f"  [reconcile] {path}: not promoting — "
                      f"reconciliation to base is a no-op",
                      file=sys.stderr)
            continue

        # Commit the exclusion set.
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

        # Safety net: re-run the same checks we relied on during the
        # search, so a bug in `_find_minimum_exclusions` cannot
        # commit bad content.
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
                # Should never happen; refuse to promote.
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
