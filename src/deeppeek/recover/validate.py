"""
recover.validate — Pass 7 (cross-validate imports) and Pass 8
(verify files).

Pass 7 is a pure read over the recovered content.

Pass 8 verifies ast.parse for .py files and checks that patches took
effect.

NEW (2026-10-30c) **Trust the patcher for `deterministic`.**

The verifier no longer re-checks `deterministic` patches.  A
deterministic patch is one that matched its anchor exactly and
mutated the exact lines it named, via a helper (`_delete_exact_block`,
`_try_raw_diff_patch`, or the object-literal splice) that is correct
by construction.  Re-checking the *final* content afterwards produces
false failures whenever a later patch on the same file edited the
same lines again.

The specific shape that broke the panel-fade conversation:

    patch 1 (deletion, "delete this block"): applies
    patch 2 (diff, deletion of the same block): sees block already
        gone, marks itself `already-present`
    verify: patch 1 is deterministic, block is gone → the
        deleted block's lines look "missing" → trust drops to LOW
    result: pg_state.py is written to .deeppeek/low_trust/ even
        though both patches succeeded, and the tree version is
        unchanged.

Verify now skips `deterministic` and `already-present` patches
entirely.  Only `llm` patches are re-checked — they can hallucinate
context and need the presence/absence gate.

The same skip applies to `already-present`: those were no-ops at
application time; the content may have evolved since.

The diff-aware and deletion-aware checks remain for `llm` patches.

Three false-positive classes were eliminated in pass 7 earlier:
framework namespaces (`pgkit.*`), contentless records, stdlib
modules shadowing local basenames.  All are handled by
`_resolve_module` and `_is_likely_local` below, and shared with
`write._aggregate_missing_imports`.
"""

import ast
import os
import re
import sys
from pathlib import Path
from typing import Optional

from .core import (
    FileRecord, _basename,
    _is_deletion_instruction,
    _looks_like_diff_body,
    _strip_diff_prefixes,
)


def pass7_cross_validate(records: dict[str, FileRecord]) -> dict:
    exports: dict[str, set[str]] = {}
    imports: dict[str, list[tuple[str, str]]] = {}

    for path, rec in records.items():
        if rec.content is None:
            continue
        ext = Path(path).suffix.lower()
        if ext == ".py":
            exports[path] = _py_exports(rec.content)
            imports[path] = _py_imports(rec.content)

    problems: list[dict] = []
    seen_missing_modules: set[tuple[str, str]] = set()
    seen_missing_exports: set[tuple[str, str, str]] = set()

    for path, imps in imports.items():
        for module, name in imps:
            target = _resolve_module(module, records, path)
            if target is None:
                if _is_likely_local(module, records):
                    display = module.lstrip(".")
                    if not display:
                        continue
                    key = (path, display)
                    if key in seen_missing_modules:
                        continue
                    seen_missing_modules.add(key)
                    problems.append({
                        "type": "missing_module",
                        "from": path,
                        "module": display,
                    })
                continue
            if name not in exports.get(target, set()):
                key = (path, target, name)
                if key in seen_missing_exports:
                    continue
                seen_missing_exports.add(key)
                problems.append({
                    "type": "missing_export",
                    "from": path,
                    "target": target,
                    "name": name,
                })

    return {
        "exports": {p: sorted(s) for p, s in exports.items()},
        "imports": {p: [list(t) for t in imps]
                    for p, imps in imports.items()},
        "problems": problems,
    }


def _py_exports(body: str) -> set[str]:
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)):
            out.add(node.target.id)
    return out


def _py_imports(body: str) -> list[tuple[str, str]]:
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return []
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            level = node.level or 0
            mod = "." * level + (node.module or "")
            if not mod:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                out.append((mod, alias.name))
    return out


_EXTERNAL_NAMESPACES = ("pgkit", "playground_")


def _resolve_module(module: str, records: dict[str, FileRecord],
                    from_path: str) -> Optional[str]:
    if not module.startswith((".", "/")):
        top = module.split(".", 1)[0]
        if hasattr(sys, "stdlib_module_names"):
            if top in sys.stdlib_module_names:
                return None
        for ns in _EXTERNAL_NAMESPACES:
            if top == ns or top.startswith(ns + "."):
                return None

    if module.startswith("."):
        level = 0
        while level < len(module) and module[level] == ".":
            level += 1
        rest = module[level:]
        if not rest:
            return None

        parent = os.path.dirname(from_path)
        for _ in range(level - 1):
            parent = os.path.dirname(parent)

        parts = rest.split(".")

        candidate = "/".join(([parent] if parent else []) + parts)
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = candidate + ext
            rec = records.get(full)
            if rec is not None and rec.content is not None:
                return full

        last = parts[-1]
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = last + ext
            rec = records.get(full)
            if rec is not None and rec.content is not None:
                return full
        return None

    if module.startswith("/"):
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = module + ext
            rec = records.get(full)
            if rec is not None and rec.content is not None:
                return full
        return None

    for path, rec in records.items():
        if rec.content is None:
            continue
        if _basename(path) == module + ".py":
            return path
        last = module.rsplit(".", 1)[-1]
        if last != module and _basename(path) == last + ".py":
            return path
    return None


def _is_likely_local(module: str,
                     records: Optional[dict] = None) -> bool:
    if module.startswith((".", "/")):
        return True
    top = module.split(".", 1)[0]
    if records is not None:
        for ext in (".py", ".js", ".mjs", ".ts", ".tsx"):
            candidate = top + ext
            for p in records:
                if _basename(p) == candidate:
                    return True
    for ns in _EXTERNAL_NAMESPACES:
        if top == ns or top.startswith(ns + "."):
            return False
    if hasattr(sys, "stdlib_module_names"):
        if top in sys.stdlib_module_names:
            return False
    return "_" in module


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


def pass8_verify(records: dict[str, FileRecord],
                 blocks_by_idx: Optional[dict] = None) -> None:
    from .validate_output import validate_content
    # ---- global patch-conflict detection ----
    # A block applied to more than one record indicates a parser
    # or attribution bug.  Demote every affected record to LOW
    # so the fail-closed policy routes them to low_trust/ instead
    # of silently duplicating the edit in the target tree.
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

        # Declarations that a deletion patch was explicitly told to
        # remove must not trigger the presence regression check.
        # Otherwise every correct deletion looks like a lost function.
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
                for m in __import__("re").finditer(
                        r"(?:const|let|var|function|class)\s+"
                        r"([A-Za-z_$][\w$]*)",
                        stripped):
                    allow_missing.add(m.group(1))

        # Deduplicate blocks that were applied twice.  Runs on the raw
        # patched content BEFORE validate_content, so a file whose only
        # validation error was a duplicate block gets cleaned up and
        # passes instead of fail-closing.  Two classes are handled:
        # duplicate `if (COND) { ... }` with identical normalized
        # bodies in the same scope, and duplicate module-scope
        # `const NAME = ...;` (plus its trailing `if (NAME) { ... }`).
        # This is the fix for test_31 (duplicate if-guards) and for
        # the pg_panel_scrubs.py "duplicate top-level declaration"
        # fail-closed case in the real conversation.
        from .validate_output import _dedup_duplicate_blocks
        from .validate_output import _dedup_repeated_statements
        from .validate_output import _LAST_DEDUP_DETAILS
        import deeppeek.recover.validate_output as _vo

        # Skip dedup entirely for pre-existing baselines that the
        # conversation didn't touch.  Such records are never written
        # (preexisting_unmodified), so deduping them is wasted work
        # and produces a misleading count in the manifest.
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

            # Structural brace repair: suggestion only.  Fires only
            # when the failure is exactly `unbalanced braces (+N)`
            # and the block is unambiguously structural (no trailing
            # siblings, complete last statement).  Never auto-writes
            # to the target — the suggestion lands in low_trust/ for
            # user review.
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
                # Attempted but refused — record why so the manifest
                # is not silent about what specific criterion fired.
                reason = (repair_result.get("refused_reason")
                          or "refused (no reason recorded)")
                v["sanity_repair_available"] = False
                v["sanity_repair_reason"] = reason
                print(f"  [repair] {path}: refused ({reason})",
                      file=sys.stderr)
            else:
                # try_repair returned None — not a brace failure.
                v["sanity_repair_available"] = False
                v["sanity_repair_reason"] = (
                    "not applicable (not a brace-unbalance failure)")

            rec.trust = "LOW"
            rec.sanity_rejected = True
            rec.verification["sanity_reason"] = structural_err
            rec.warnings.append(
                f"content validation failed: {structural_err}")
            print(f"  [verify] content validation FAIL in {path}: "
                  f"{structural_err}", file=sys.stderr)
            continue
        v["content_valid"] = "ok"

        if path.endswith(".py"):
            try:
                ast.parse(rec.content)
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

                # --- skip already-verified or no-op patches ---
                if status == "failed":
                    continue
                if status == "already-present":
                    # No-op at application time; content may have
                    # evolved since.  Do not re-check.
                    continue
                if status == "deterministic":
                    # The deterministic patcher matched its anchor
                    # exactly and mutated the exact lines it named.
                    # Re-checking the FINAL content afterwards
                    # produces false failures whenever a later patch
                    # on the same file edits the same lines again —
                    # e.g. patch A deletes a block, patch B deletes
                    # the same block (duplicate assistant message),
                    # and patch A's verify then reports "the block
                    # is missing" (which it is — that's the point).
                    # Trust the patcher.
                    continue

                # Anything else (llm) gets the full verification.

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
