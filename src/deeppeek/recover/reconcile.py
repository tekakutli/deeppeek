"""
recover.reconcile — Pass 8.7: reconcile fail-closed records by
excluding the minimum set of patches/pairs that caused the failure.

See the pre-fix module docstring for the motivating cases and the
"reconciled to base is not a rescue" invariant.

STALE-WARNING CLEANUP
=====================
Pass 6 appends `patch idx={idx} could not be applied (...)` warnings
for every failed patch.  When reconciliation excludes a patch and
promotes the record, those warnings are now stale — the patch is
`skipped_unplaceable` or `skipped_misattributed`, not "failed".
`pass8_7_reconcile` removes the pass-6 warning for each excluded
patch idx before adding its own reconciliation warning.

THREE MORE CLASSES OF STALE WARNING
===================================
`content validation failed` from pass 8, `replace pair(s) failed`
from pass 6, and `unresolved symbol reference(s) introduced by patch`
from pass 8.5 get the same treatment: when reconcile rewrites their
live state, the aggregate warning is removed.

ORDERING FIXES
==============
  · `_diagnose_exclusion` now receives the pre-mutation status via
    `original_status`, so the "could not be placed" branch is
    reachable.  Prior versions mutated the status first and read it
    after, so the generic "unresolved symbol" message always fired.

  · `_trust_cap(rec)` is called *after* `rec.verification
    ["reconciled"]` is populated.  Prior versions called it before,
    so reconciled records always saw HIGH instead of the documented
    MEDIUM cap.

LINT-AWARE REPLAY (2026-10-28b)
===============================
`_count_failures` now runs pass 8.4's `_find_command_violations` on
each candidate replay.  Without this, a record demoted by pass 8.4
for an unused `@command` arg could be *promoted* by pass 8.7 if
reconcile's own two checks (validate_content + symbols) happened to
pass on the replayed content — even though the replayed content still
declares the arg without reading it.  The result: a semantically
broken file ships at MEDIUM trust.

Motivating case (comfyui.py): `--output_rotation` was declared by a
patch that applied; the wiring patch was excluded.  Pass 8.4
correctly demoted to LOW.  Pass 8.7 replayed the content, saw a
valid Python file with all symbols resolved, and promoted to MEDIUM
— shipping the dead arg.  The fix: reconcile's replay now respects
every demoting pass, not just the two it was originally written
against.  This is the general invariant — a check that can demote
must be re-run after any pass that can promote.

DISTINCTIVE SCOPE-NAME MISATTRIBUTION (2026-10-28b)
===================================================
`_fuzzy_locate_elsewhere` classifies an excluded patch as "likely
misattributed" by checking whether its content already exists in a
different record.  The original check used line-overlap thresholds
(verbatim for short bodies, 0.85 for long) — which missed the
`_clearImageAction` case: a function whose body was modified by
three new lines dropped overlap below 0.85, so the misattribution
was reported as an opaque "unresolved symbol" instead of "the body
lives in pg_panel_bindings.py".

New fast path: if the body declares *exactly one* top-level scope
whose name is distinctive (a function/class, not a plain variable)
AND that scope is also declared in another record AND the bodies
overlap at 0.4, the other record is returned directly.

"Distinctive" excludes short/generic names (`draw`, `_get`, `run`)
that legitimately appear across playground files; a body with
`_clearImageAction` or `syncPanelFields` is almost certainly
specific to one file, so a same-named scope elsewhere is a strong
misattribution signal.

"SINGLE SCOPE" is load-bearing
------------------------------
The fast path requires that `_top_level_decls(body)` returns exactly
one name.  A multi-decl body (a cluster of 10 functions, a class
plus its helpers) is bulk content, not the "one function whose body
lives elsewhere" shape.  Without this guard, a body of 10 helper
functions with similar names (helper_0..helper_9) trips the path
against an unrelated file that happens to have helper_0..helper_7,
even though a whole-file 0.8 similarity is not the kind of
misattribution the fast path exists to catch.  This guard is the
fix for the test_patch_gates.py::test_fuzzy_long_body_uses_higher_
threshold regression: the test's intent (0.85 for long bodies) is
preserved for multi-scope bodies; the fast path only short-circuits
it for single-scope bodies, where it is a much stronger signal.

MISATTRIBUTION GUARD (raised thresholds)
========================================
`_fuzzy_locate_elsewhere` classifies an excluded patch as "likely
misattributed" (content lives in a different file already) versus
"could not be placed".  The threshold split:

  · Short bodies (< 500 chars) require *verbatim* substring match.
    Short bodies are idioms (try/except, arg-parse) that recur in
    many files; a fuzzy hit at this length is not evidence of
    misattribution.  Motivating case: a 4-line try/except block
    matching a different file's try/except by 3-of-4 lines, where
    the differing line names a different variable (`params` vs
    `payload`) — that is not the same block.

  · Substantial bodies (>= 500 chars) use fuzzy match at 0.85.
    A 15% line drift on a large function is consistent with a
    legitimate-but-different implementation; raising the threshold
    (from 0.7) is conservative.
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
    """Return (n_failures, [failure_descriptors]).

    Runs every demoting check pass 8.x performs, so a promotion by
    reconcile is only as strong as the *weakest* re-verified check.
    See the module docstring, LINT-AWARE REPLAY section.
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

    # LINT-AWARE (2026-10-28b): re-run pass 8.4's unused-arg check on
    # the replayed content.  See module docstring.
    if path.endswith(".py") and "@command" in content:
        try:
            from .lint import (
                _find_command_violations, _arg_introduced_by_patch,
            )
            violations = _find_command_violations(content)
        except Exception:
            violations = []
        for v in violations:
            introduced = [a for a in v["unused"]
                          if _arg_introduced_by_patch(rec, a)]
            if introduced:
                failures.append(("unused_command_args", {
                    "method": v["name"],
                    "args":   introduced,
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
    """True iff `name` is long enough and distinctive enough that a
    same-named scope in another file is a strong misattribution
    signal.

    Excluded: short names (`draw`, `_get`, `run`), and names without
    an underscore or camelCase (which is most generic verbs).  The
    filter targets the motivating case (`_clearImageAction` — 17
    chars, has an underscore) while avoiding false positives on
    common helper names shared across playground files.
    """
    if not name or len(name) < 8:
        return False
    return "_" in name or any(c.isupper() for c in name)


def _scope_name_lives_elsewhere(body, current_path, records):
    """If `body` declares *exactly one* top-level scope whose name is
    distinctive AND that scope is also declared in another record AND
    the two bodies overlap at 0.4, return the other record's path.
    Else None.

    SINGLE-SCOPE GUARD (2026-10-28b, fixes regression)
    --------------------------------------------------
    The fast path requires that the body declare exactly one
    top-level scope.  A multi-decl body is bulk content, not the
    "one function whose body lives elsewhere" shape — the fast path
    is a signal for the case where a *single* function was misplaced.

    Without this guard, a body of 10 helper functions
    (helper_0..helper_9) trips the path against an unrelated file
    that has helper_0..helper_7, since the body's first extracted
    scope name (helper_0) matches and line overlap exceeds 0.4.
    That defeats the 0.85 threshold for long bodies that
    `test_fuzzy_long_body_uses_higher_threshold` locks in.  The
    guard restores the threshold for multi-scope bodies while
    keeping the fast path for single-scope bodies where it is
    genuinely diagnostic.

    Overlap threshold 0.4 — the `_clearImageAction` shape has 5 of 7
    lines in common (0.71), so 0.4 is comfortable.  `syncPanelFields`
    across two playgrounds (two different sets of element wiring)
    overlaps under 0.4 and correctly fails the fast path, falling
    through to the general 0.85 check.
    """
    from .prose import _extract_scope_from_body
    from .patch_text import _content_present_in_base
    from .dedup import _top_level_decls

    scope_name, _ = _extract_scope_from_body(body)
    if not _is_distinctive_scope_name(scope_name):
        return None

    # Single-scope guard: a multi-decl body is not a misattribution
    # signal for any one of its scopes.  See the docstring above.
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

    Order of checks:

      1. Distinctive-scope-name match (2026-10-28b): the body declares
         a single distinctive function/class/<details> whose same-
         named scope also lives in another record AND the two bodies
         overlap meaningfully.  See module docstring, DISTINCTIVE
         SCOPE-NAME MISATTRIBUTION section.

      2. Existing verbatim / threshold logic: short bodies require
         verbatim substring; substantial bodies use fuzzy match at
         0.85.  See module docstring, MISATTRIBUTION GUARD section.
    """
    from .patch_text import _content_present_in_base
    body = (body or "").strip()
    if not body or len(body) < 40:
        return None

    # NEW (2026-10-28b): distinctive scope-name fast path.
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

    `original_status` overrides `patch.get("applied_by")` when the
    caller has already mutated the patch status.  Without this, the
    "could not be placed" branch never fires because the status is
    already "skipped_unplaceable" by the time we read it.
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
    """Trust ceiling for a record after reconciliation.

    A reconciled record caps at MEDIUM: reconciliation excluded the
    evidence of a problem, not the problem itself.  MEDIUM (not LOW)
    so the file still writes under default `--min-trust MEDIUM`, but
    the exclusion is visible in the manifest.

    The precondition — that `rec.verification["reconciled"]` is set
    before this function is called — is the caller's job.
    """
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


_PASS6_PATCH_WARN_PREFIX = "patch idx="


def _clear_stale_pass6_warnings(rec, excluded_keys):
    """Remove warnings that are stale after reconciliation.  See the
    module docstring for the four warning classes cleaned up."""
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

        if w.startswith(
                "unresolved symbol reference(s) introduced by patch:"):
            if not rec.verification.get("unresolved_calls"):
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

        # Capture original statuses before mutation so the
        # diagnostic still knows which patches genuinely failed.
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
