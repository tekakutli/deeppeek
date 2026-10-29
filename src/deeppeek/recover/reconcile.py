"""
recover.reconcile — Pass 8.7: reconcile fail-closed records by
excluding the minimum set of patches/pairs that caused the failure.

See the pre-fix module docstring for the motivating cases and the
"reconciled to base is not a rescue" invariant.

PATH THREADING (2026-10-29)
===========================
`_replay_excluding` now passes `path=path` to `_apply_pairs`, so
scope replaces executed during reconcile replay get AST-snapped
boundaries and structural validation — the same treatment they got
during the initial pass-6 application.  Without this, reconcile
could not replay a scope-replace that pass 6 applied correctly, and
would silently produce different results during its exclusion
search.

PRIOR-PASS ANNOTATION INTERACTION (2026-10-29)
==============================================
Pass 6 now downgrades `deterministic` patches to `failed` when the
record's final content is structurally invalid (see
`apply_patches.pass6_apply_patches`).  Reconcile's `_applied_keys`
sees the failed status and its `_find_minimum_exclusions` can try
excluding them.  When a single patch caused the corruption, this
promotes the record; when multiple did, it stays LOW (correct
fail-closed).

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


def _replay_excluding(rec, base, excluded_keys, path,
                      blocks_by_idx, records):
    """Replay pass-6 application with `excluded_keys` removed.

    `path` is passed to `_apply_pairs` so scope replaces executed
    during replay get AST-snapped boundaries (the same treatment as
    the initial application).  Without this, replay could not
    reproduce the pass-6 result and reconcile's exclusion search
    would fail for every pair-carrying record.
    """
    content = base

    pairs_kept = [
        pair for pair in rec.pairs
        if _pair_key(pair) not in excluded_keys
    ]
    if pairs_kept:
        content, _ = _apply_pairs(content, pairs_kept, path=path)

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
    """Return (n_failures, [failure_descriptors]).

    Runs every *demoting* check pass 8.x performs.  Lint and
    added-param-lint are NOT included: both are warn-only (see
    `lint.py` and `verify_applied.py` docstrings).  Only checks that
    actually change `rec.trust` participate here, so reconcile's
    promotion decision is not blocked by informational warnings.
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


def _diagnose_exclusion(key, rec, records, original_status=None):
    """Produce a human-readable exclusion message.

    Three distinct cases:

      1. `original_status == "failed"` — never applied.  "Could not
         be placed."
      2. `_fuzzy_locate_elsewhere` finds a match — applied but
         belongs elsewhere.  "Content already exists in X;
         misattributed."
      3. Neither — applied but produces a validation failure that
         couldn't be attributed.  "Introduces X; content not found
         elsewhere."
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
        if status == "failed":
            return (f"excluded patch idx={idx} ({first!r}): "
                    f"could not be placed; the rest of the record "
                    f"applies cleanly without it")
        other = _fuzzy_locate_elsewhere(
            patch.get("body", ""), rec.path, records)
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

        for key in excluded_keys:
            original_status = None
            if key[0] == "patch":
                original_status = original_statuses.get(key[1])
            diag = _diagnose_exclusion(
                key, rec, records, original_status=original_status)
            rec.warnings.append(diag)
            if verbose:
                print(f"  [reconcile] {path}: {diag}", file=sys.stderr)

        n_reconciled += 1
        if verbose:
            print(f"  [reconcile] {path}: reconciled "
                  f"({len(excluded_keys)} exclusion(s); "
                  f"trust={rec.trust})", file=sys.stderr)

    return n_reconciled
