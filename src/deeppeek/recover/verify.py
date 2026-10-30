"""
recover.verify — Pass 8 (verify files) and Pass 8.5 (symbol check).

Extracted from the pre-2026-11-05 `validate.py`, which combined two
conceptually distinct concerns:

  · Pass 7 family: read-only import/export cross-validation, called
    before pass 8 has run.  Stable.

  · Pass 8 family: post-apply verification, called after pass 6 has
    mutated `rec.content`.  Has the churny bits (dedup, sanity
    attribution, brace-repair suggestions, symbol resolution).

The split is clean — no helper is shared between the two families.
`validate.py` continues to re-export the pass-8 names under their
pre-split names, so existing imports keep resolving.

TRUST THE PATCHER FOR `deterministic`
=====================================
The verifier does not re-check `deterministic` patches.  A
deterministic patch is one that matched its anchor exactly and
mutated the exact lines it named.  Re-checking the final content
afterwards produces false failures whenever a later patch on the
same file edited the same lines again.

PRE-EXISTING SHORT-CIRCUIT (2026-11-05)
=======================================
`pass8_verify` skips structural validation for records whose
`verification["preexisting_unchanged"]` flag is set.  `pass6` sets
that flag on records whose source blocks include a pre-existing
baseline and whose patch list is empty (patches=[] and pairs=[]).
Those records have `content == base`, and `base` is a file the
user has on disk and is working from — there is nothing to
validate.

Motivating case: the 2026-10-29 `--apply-to` run spent ~40 seconds
validating ~280 untouched pre-existing `.py` files.  Every file got
two `ast.parse` calls in `_module_level_stmts_added` plus another
two in `_function_presence_check`.  The flag cuts all of it.

Pass 8.5 — symbol resolution
============================
`pass8_5_check_symbols` cross-references patch-introduced bare calls
against each file's imports and declarations.  Demote the record to
LOW trust on an unresolved call introduced by a patch.

SANITY ATTRIBUTION CROSS-RECORD GATE
====================================
`attribute_sanity_failure` (from `apply_patches.py`) can
occasionally return an idx from a *different* record's patch set.
`_validate_sanity_attribution` gates the returned idx against the
record's own patch set.

POST-VALIDATION PARSE CACHE (2026-11-05)
========================================
`pass8_verify`'s post-validation `ast.parse(rec.content)` now routes
through `parse_source`, so a `.py` record whose content was already
parsed by an earlier pass (dedup, symbol check, structural diff)
reuses the cached tree instead of re-parsing.  The parse was
uncached before and dominated the verify pass's per-record cost on
`--apply-to` runs.

`_try_repair_python` still uses `ast.parse` for its candidate
repairs — each candidate is a unique string, so the cache never
hits, and the inputs are small (a file body + a few-char suffix).
"""

import ast
import re
import sys
from typing import Optional

from .core import (
    FileRecord,
    _is_deletion_instruction,
    _strip_diff_prefixes,
    _looks_like_diff_body,
)
from ._ast import parse_source


# ============================================================================
#  Pass 8 — verify files
# ============================================================================

_REPAIR_TERMINATORS = frozenset(';})]"\'>')


def _try_repair_python(body: str, err: SyntaxError) -> Optional[str]:
    stripped = body.rstrip()
    if not stripped:
        return None

    if stripped[-1] not in _REPAIR_TERMINATORS:
        return None

    candidates = [
        stripped + '\n"""',
        stripped + "\n'''",
        stripped + "\n)",
        stripped + "\n}",
        stripped + "\n]",
        stripped + "\n);",
        stripped + '\n"""\n',
    ]
    for cand in candidates:
        try:
            ast.parse(cand)
            return cand
        except SyntaxError:
            continue
    return None


_STRUCTURAL_PATCH_EXPORT_RE = re.compile(
    r'^export\s+(?:const|let|var)\s+\w+\s*=\s*\{')


def _has_structural_failures(rec: FileRecord) -> bool:
    for p in rec.patches:
        if p.get("applied_by") != "failed":
            continue
        body = p["body"].strip()
        if not body:
            continue
        first_line = body.split("\n", 1)[0].strip()
        if _STRUCTURAL_PATCH_EXPORT_RE.match(first_line):
            return True
        if body.startswith("@@ ") or "\n@@ " in body:
            return True
    return False


def _diff_lines_for_verify(body: str) -> tuple[list[str], list[str]]:
    adds: list[str] = []
    dels: list[str] = []
    for pl in body.splitlines():
        if pl.startswith("+") and not pl.startswith("+++"):
            content = pl[1:].rstrip()
            if content.strip():
                adds.append(content)
        elif pl.startswith("-") and not pl.startswith("---"):
            content = pl[1:].rstrip()
            if content.strip():
                dels.append(content)
    return adds, dels


def _validate_sanity_attribution(rec: FileRecord,
                                 idx: Optional[int]) -> Optional[int]:
    """Return `idx` if it names a patch in `rec.patches`, else None."""
    if idx is None:
        return None
    for p in rec.patches:
        if p.get("idx") == idx:
            return idx
    return None


def pass8_verify(records: dict[str, FileRecord],
                 blocks_by_idx: Optional[dict] = None) -> None:
    from .validate_output import validate_content

    # ---- global patch-conflict detection ----
    patch_usage = {}
    for _p, _r in records.items():
        for _pp in _r.patches:
            _i = _pp.get("idx")
            if _i is None:
                continue
            patch_usage.setdefault(_i, []).append(_p)
    for _p, _r in records.items():
        if _r.content is None:
            continue
        _v = _r.verification
        _conflicts = []
        for _pp in _r.patches:
            _i = _pp.get("idx")
            if _i is None:
                continue
            _others = [op for op in patch_usage.get(_i, []) if op != _p]
            if _others:
                _conflicts.append({
                    "patch_idx": _i,
                    "other_records": _others,
                })
        if _conflicts:
            _v["patch_conflict"] = _conflicts
            _r.trust = "LOW"
            _r.warnings.append(
                "patch-conflict: %d block(s) claimed by another "
                "record - likely a duplicated source block"
                % len(_conflicts))
            print(f"  [verify] {_p}: {len(_conflicts)} patch(es) "
                  f"cross-claimed by another record; "
                  f"demoting to LOW", file=sys.stderr)

    for path, rec in records.items():
        if rec.content is None:
            continue
        v = rec.verification

        base_for_check = None
        if blocks_by_idx is not None and rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(rec.last_full_idx)
            if base_block is not None:
                base_for_check = base_block.body

        # NEW (2026-11-05): skip the whole validation pass for
        # pre-existing records that were not modified.  `pass6`
        # short-circuits them and sets this flag; there is nothing
        # to validate — content == base, and base is a file the
        # user has on disk and is working from.
        if rec.verification.get("preexisting_unchanged"):
            v["content_valid"] = "ok"
            if path.endswith(".py"):
                v["ast_parse"] = "ok"
            continue

        allow_missing = set()
        if base_for_check:
            from .core import _is_deletion_instruction, _strip_diff_prefixes
            for p in rec.patches:
                if p.get("applied_by") != "deterministic":
                    continue
                body = p.get("body", "")
                if not body:
                    continue
                if not _is_deletion_instruction(
                        body, p.get("context_before", "")):
                    continue
                stripped = _strip_diff_prefixes(body)
                for m in re.finditer(
                        r"(?:const|let|var|function|class)\s+"
                        r"([A-Za-z_$][\w$]*)",
                        stripped):
                    allow_missing.add(m.group(1))

        from .validate_output import _dedup_duplicate_blocks
        from .validate_output import _dedup_repeated_statements
        from .validate_output import _LAST_DEDUP_DETAILS
        import deeppeek.recover.validate_output as _vo

        _skip_dedup = (
            rec.preexisting_base
            and not rec.pairs
            and not any(
                p.get("applied_by") != "already-present"
                for p in rec.patches
            )
        )

        if _skip_dedup:
            n_removed = 0
            _dedup_details = []
        else:
            _vo._LAST_DEDUP_DETAILS = []
            deduped, n_removed = _dedup_duplicate_blocks(
                rec.content, path)
            _dedup_details = list(_vo._LAST_DEDUP_DETAILS)
            _patch_bodies = [p.get("body", "") for p in rec.patches]
            _vo._LAST_DEDUP_DETAILS = []
            deduped2, n_removed2 = _dedup_repeated_statements(
                deduped, path,
                base=base_for_check,
                patch_bodies=_patch_bodies)
            _dedup_details.extend(_vo._LAST_DEDUP_DETAILS)
            if n_removed2 > 0:
                deduped = deduped2
                n_removed += n_removed2

        if n_removed > 0:
            rec.content = deduped
            rec.verification["deduped_blocks"] = n_removed
            if _dedup_details:
                rec.verification["deduped_detail"] = _dedup_details
            print(f"  [dedup] {path}: removed {n_removed} duplicate "
                  f"block(s)", file=sys.stderr)

        structural_err = validate_content(
            rec.content, path, base=base_for_check,
            allow_missing=allow_missing)
        if structural_err:
            v["content_valid"] = f"FAIL: {structural_err}"

            repair_result = None
            repair_raised = None
            if blocks_by_idx is not None:
                try:
                    from .repair import try_repair
                    repair_result = try_repair(
                        rec=rec,
                        structural_err=structural_err,
                        blocks_by_idx=blocks_by_idx,
                        path=path,
                    )
                except Exception as e:
                    repair_raised = str(e)
                    print(f"  [repair] {path}: repair attempt "
                          f"raised: {e}", file=sys.stderr)

            if repair_raised is not None:
                v["sanity_repair_available"] = False
                v["sanity_repair_reason"] = (
                    f"repair attempt raised: {repair_raised}")
            elif repair_result is not None and \
                 repair_result.get("repaired_content"):
                rec.repair_content = repair_result["repaired_content"]
                v["sanity_repair_available"] = True
                v["sanity_repair_closers"] = repair_result["closers_added"]
                v["sanity_repair_reason"] = None
                print(f"  [repair] {path}: suggestion produced "
                      f"(+{repair_result['closers_added']} closer(s))",
                      file=sys.stderr)
            elif repair_result is not None:
                reason = (repair_result.get("refused_reason")
                          or "refused (no reason recorded)")
                v["sanity_repair_available"] = False
                v["sanity_repair_reason"] = reason
                print(f"  [repair] {path}: refused ({reason})",
                      file=sys.stderr)
            else:
                v["sanity_repair_available"] = False
                v["sanity_repair_reason"] = (
                    "not applicable (not a brace-unbalance failure)")

            rec.trust = "LOW"
            rec.sanity_rejected = True

            _culprit = None
            if rec.patches and blocks_by_idx is not None:
                try:
                    from .apply_patches import attribute_sanity_failure
                    _raw = attribute_sanity_failure(rec, blocks_by_idx)
                except Exception:
                    _raw = None
                _culprit = _validate_sanity_attribution(rec, _raw)
            if _culprit is not None:
                rec.verification['sanity_caused_by'] = _culprit
                _w = (f"content validation failed: {structural_err} "
                      f"(patch idx={_culprit} caused it)")
            else:
                _w = (f"content validation failed: {structural_err} "
                      f"(patch attribution unavailable — likely "
                      f"depends on prior_insertions)")
            rec.verification["sanity_reason"] = structural_err
            rec.warnings.append(_w)
            print(f"  [verify] content validation FAIL in {path}: "
                  f"{structural_err}", file=sys.stderr)
            if _culprit is not None:
                print(f"  [verify]   caused by patch idx={_culprit}",
                      file=sys.stderr)
            continue
        v["content_valid"] = "ok"

        if path.endswith(".py"):
            try:
                parse_source(rec.content, filename=path)
                v["ast_parse"] = "ok"
            except SyntaxError as e:
                repaired = _try_repair_python(rec.content, e)
                if repaired is not None:
                    rec.content = repaired
                    v["ast_parse"] = "ok (auto-repaired)"
                    rec.warnings.append(
                        f"ast.parse failed at line {e.lineno} "
                        f"({e.msg}); auto-repaired by appending a "
                        f"plausible closer")
                    print(
                        f"  [verify] ast.parse FAIL in {path} — "
                        f"auto-repaired (was: line {e.lineno}: {e.msg})",
                        file=sys.stderr,
                    )
                else:
                    msg = f"line {e.lineno}: {e.msg}"
                    v["ast_parse"] = f"FAIL: {msg}"
                    rec.trust = "LOW"
                    rec.warnings.append(f"ast.parse failed: {msg}")
                    print(f"  [verify] ast.parse FAIL in {path}: {msg}",
                          file=sys.stderr)

        if (path.endswith(".py")
                and rec.dedup_reason == "last_full"
                and rec.trust == "MEDIUM"
                and v.get("ast_parse", "").startswith("ok")):
            if v.get("patches_incomplete") and _has_structural_failures(rec):
                v["last_full_promotion_blocked"] = (
                    "structural patch failure(s)")
                print(f"  [verify] {path}: last_full promotion blocked "
                      f"(structural patch failure)",
                      file=sys.stderr)
            else:
                rec.trust = "HIGH"
                v["last_full_promoted"] = True
                if v.get("patches_incomplete"):
                    rec.warnings.append(
                        "last_full promoted to HIGH after ast.parse "
                        "OK; failed patches were non-structural "
                        "(prose snippets) — see verification")
                    print(f"  [verify] {path}: last_full promoted to "
                          f"HIGH (ast.parse OK, failed patches are "
                          f"non-structural)",
                          file=sys.stderr)

        # ---- patch_presence ----
        if rec.patches and rec.content:
            content_lines = {
                ln.rstrip() for ln in rec.content.splitlines()
                if ln.strip()
            }
            content_lines_nonempty = [
                ln.rstrip() for ln in rec.content.splitlines()
                if ln.strip()
            ]

            total_missing_adds = 0
            total_deletions_not_removed = 0

            for patch in rec.patches:
                status = patch.get("applied_by")

                if status == "failed":
                    continue
                if status == "already-present":
                    continue
                if status == "skipped_weak_attribution":
                    continue
                if status == "deterministic":
                    continue
                if status == "region-replace":
                    continue

                body = patch["body"]

                if _looks_like_diff_body(body):
                    adds, dels = _diff_lines_for_verify(body)

                    for a in adds:
                        if a not in content_lines:
                            total_missing_adds += 1

                    if dels:
                        n = len(dels)
                        block_still_present = False
                        for i in range(
                                len(content_lines_nonempty) - n + 1):
                            if content_lines_nonempty[i:i + n] == dels:
                                block_still_present = True
                                break
                        if block_still_present:
                            total_deletions_not_removed += 1

                    continue

                if _is_deletion_instruction(
                        body, patch.get("context_before", "")):
                    stripped = _strip_diff_prefixes(body)
                    body_lines = [
                        ln.rstrip() for ln in stripped.splitlines()
                        if ln.strip()
                    ]
                    if body_lines:
                        n = len(body_lines)
                        for i in range(
                                len(content_lines_nonempty) - n + 1):
                            if content_lines_nonempty[i:i + n] == body_lines:
                                total_deletions_not_removed += 1
                                break
                    continue

                for pl in body.splitlines():
                    s = pl.strip()
                    if not s:
                        continue
                    if s.startswith(("//", "#")):
                        continue
                    if s not in rec.content:
                        total_missing_adds += 1

            if total_deletions_not_removed:
                v["patch_presence"] = (
                    f"{total_deletions_not_removed} deletion block(s) "
                    f"still present"
                )
                rec.warnings.append(
                    f"patch presence: "
                    f"{total_deletions_not_removed} deletion block(s) "
                    f"that should have been removed are still in the "
                    f"file"
                )
                rec.trust = "LOW"
                print(f"  [verify] patch presence FAIL in {path}: "
                      f"{total_deletions_not_removed} deletion block(s) "
                      f"did not take effect", file=sys.stderr)
            elif total_missing_adds:
                v["patch_presence"] = (
                    f"{total_missing_adds} missing lines")
                rec.warnings.append(
                    f"patch presence: {total_missing_adds} lines "
                    f"not found")
                rec.trust = "LOW"
                print(f"  [verify] patch presence FAIL in {path}: "
                      f"{total_missing_adds} missing lines",
                      file=sys.stderr)
            else:
                v["patch_presence"] = "ok"


# ============================================================================
#  Pass 8.5 — symbol resolution
# ============================================================================

def pass8_5_check_symbols(records: dict[str, FileRecord],
                          blocks_by_idx: Optional[dict] = None) -> None:
    from .symbols import find_unresolved_references

    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.is_deleted:
            continue
        if not (rec.patches or rec.pairs):
            continue

        base = None
        if blocks_by_idx is not None and rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(rec.last_full_idx)
            if base_block is not None:
                base = base_block.body

        unresolved, mismatched = find_unresolved_references(
            rec, base, records)
        if not unresolved and not mismatched:
            continue

        if unresolved:
            rec.verification["unresolved_calls"] = unresolved
        if mismatched:
            rec.verification["signature_mismatches"] = [
                {"name": n, "detail": d} for n, d in mismatched
            ]

        rec.trust = "LOW"
        parts = []
        if unresolved:
            parts.append(
                "unresolved symbol reference(s): " + ", ".join(unresolved))
        if mismatched:
            parts.append(
                "signature mismatch(es): " + "; ".join(
                    f"{n}: {d}" for n, d in mismatched))
        rec.warnings.append(
            "patch compatibility check failed: " + " | ".join(parts)
            + " (patch almost certainly targeted the wrong file or "
              "depends on an excluded patch)")

        preview = ", ".join(
            [f"{n} (unresolved)" for n in unresolved[:3]]
            + [f"{n} ({d})" for n, d in mismatched[:3]]
        )
        print(f"  [symbols] {path}: {preview}", flush=True)
