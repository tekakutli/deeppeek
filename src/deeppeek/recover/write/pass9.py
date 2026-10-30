"""recover.write.pass9 — Pass 9 (write files, manifest, warnings).

The main loop: decide per record whether to write, write to
`low_trust/`, skip, or reject; collect the warnings stream; build the
decisions dict; call `build_manifest` and `print_summary` (from
sibling modules) and write their outputs.

Historical documentation (all the "New in 2026-XX" annotations that
used to sit in `write.py`'s module docstring) is preserved below,
grouped by which section of `pass9_write` they affect.

CHURN
=====
VERY CHURNY.  Every new skip branch, every new warnings line, every
new verification field lands in this file's main loop.  That is the
whole reason the rest of `write/` was factored out — this file should
look like a linear decision tree, with the leaf actions (write,
manifest, summary) delegated to stable siblings.

HISTORY
=======

New in 2026-11-01: `sanity_rejected` branch.  A file marked with
`sanity_rejected = True` by `apply_patches` is treated exactly like
a `LOW`-trust file: not written to the target tree, reported in
`warnings.txt`, and preserved as-is.

New in 2026-11-01 (c): `patch_failure_reasons` in
`decisions[path]`.  Each failed patch records its `idx`,
`first_line`, `context_before`, and `reason`.

New in 2026-11-01 (d): `patch_failure_reasons` also records
`context_before` (truncated to 800 chars).  When a heuristic refuses
a patch because the prose didn't match, the manifest carries the
actual prose the heuristic saw.

New in 2026-11-01 (f): reconciliation feedback.  A
`reconciled_to_base` record — one where pass 8.7 excluded every
applied item and the record's content equals the pre-existing base —
is skipped silently.  `preexisting_unmodified` now treats
`skipped_misattributed` and `skipped_unplaceable` as no-op statuses
(alongside `already-present`).

New in 2026-10-28e: centralised placement-failure reporting.
`patch_failures` and `patch_failure_reasons` use the shared
`_PLACEMENT_FAILURES` set from `core.py`, so
`skipped_structural_damage` is reported alongside `failed` and
`skipped_unplaceable`.

PRE-EXISTING SKIP AGGREGATION (2026-11-05)
==========================================
The `preexisting_unmodified` branch no longer emits one
`[PREEXISTING]` line per pre-existing file.  A single aggregate
`[preexisting] N file(s) left unchanged` line is written near the
end of `warnings.txt`.  Per-file detail lives in
`manifest.decisions[path]`.

TOOL_BUG WARNINGS TAG (2026-10-29)
==================================
When a patch carries `patch["tool_bug"]` (recorded by
`_try_deterministic_patch`'s `_TOOL_BUG_EXCEPTIONS` branch), the
warnings writer emits `[TOOL_BUG]` instead of `[PATCH_FAILED]`, and
the manifest's `patch_failure_reasons` entry gains a `tool_bug`
field.  The fail-closed summary uses `TOOL_BUG` as its tag.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional

from ..core import (
    Block, FileRecord,
    SKIP_MISSING_FILES,
    _PLACEMENT_FAILURES,
    _basename,
)
from ..llm import LLMClient
from ._text import (
    _orphan_first_line,
    _patch_first_line,
    _patch_context,
    _trust_reason,
    _aggregate_warnings,
)
from ._scan import (
    _scan_mentioned_files,
    _aggregate_missing_imports,
    _recovered_name_prefixes,
    _recovered_extensions,
)
from ._delete import _apply_deletions
from ._manifest import build_manifest
from ._summary import print_summary


_NO_OP_STATUSES = frozenset({
    "already-present",
    "skipped_misattributed",
    "skipped_unplaceable",
})


def pass9_write(records: dict,
                blocks: list,
                deleted_files: list,
                out_dir: Path,
                layout: str,
                min_trust: str,
                dump_display: str,
                dump_size: int,
                dump_sha: str,
                dump_text: str,
                llm: Optional[LLMClient],
                llm_mode: str,
                cross_validate: dict,
                apply_to: Optional[Path] = None,
                resolutions: Optional[list] = None,
                cross_file_duplicates: Optional[list] = None,
                verbose: bool = False,
                run_errors: Optional[list] = None,
                pass_timings: Optional[dict] = None,
                on_disk_names: Optional[set] = None) -> int:
    # ------------------------------------------------------------------
    #  Path setup
    # ------------------------------------------------------------------
    if apply_to is not None:
        files_dir = apply_to
        aux_dir = apply_to / "deeppeek"
        low_dir = aux_dir / "low_trust"
        reports_dir = aux_dir
    else:
        files_dir = out_dir / "files"
        low_dir = out_dir / "files_low_trust"
        aux_dir = out_dir / "llm_audit"
        reports_dir = out_dir

    if not records:
        print("  [!] pipeline produced zero file records — the "
              "conversation may have had no parseable code blocks, "
              "OR an upstream pass silently discarded them.  Check "
              "the block list and warnings.txt.",
              file=sys.stderr)

    _run_errors = run_errors or []

    files_dir.mkdir(parents=True, exist_ok=True)
    aux_dir.mkdir(parents=True, exist_ok=True)

    pre_existing_files: set = (
        {p.name for p in files_dir.iterdir() if p.is_file()}
        if files_dir.exists() else set()
    )
    pre_existing_low: set = (
        {p.name for p in low_dir.iterdir() if p.is_file()}
        if low_dir.exists() else set()
    )

    trust_rank = {"SKIP": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
    min_rank = trust_rank[min_trust]

    warnings_lines: list = []
    for _err in _run_errors:
        warnings_lines.append(
            f"[PASS_FAILED] {_err['pass']}: {_err['error']}")
        for _line in (_err.get("traceback") or "").splitlines():
            warnings_lines.append(f"  {_line}")

    written: dict = {}
    skipped: list = []
    written_count = 0
    exit_code = 0
    written_out_names: dict = {}
    actually_written: set = set()
    actually_written_low: set = set()
    preexisting_skipped_count = 0

    # ------------------------------------------------------------------
    #  Main loop — one record at a time
    # ------------------------------------------------------------------
    for path, rec in sorted(records.items()):
        if layout == "flat":
            out_name = os.path.basename(path)
        else:
            out_name = path

        for w in rec.warnings:
            warnings_lines.append(f"[{path}] [WARN] {w}")

        for r in rec.verification.get("pair_records", []):
            if r.get("applied_by") != "failed":
                continue
            pair = r.get("pair", {})
            oi = pair.get("old_idx")
            ni = pair.get("new_idx")
            scope = pair.get("scope_hint", "replace")
            reason = r.get("reason", "unknown")
            kind = "scoped" if oi == -1 else "replace"

            if "ellipsis" in reason.lower():
                warnings_lines.append(
                    f"[{path}] [PAIR_ELIDED] {kind} pair "
                    f"(old={oi}, new={ni}): {reason}")
            elif ("not found in base" in reason
                  or "state drift" in reason):
                warnings_lines.append(
                    f"[{path}] [STATE_DRIFT] {kind} pair "
                    f"(old={oi}, new={ni}): {reason}")
                warnings_lines.append(
                    f"[{path}] [STATE_DRIFT] this usually means "
                    f"the base is missing a prerequisite change "
                    f"from an earlier conversation; verify the "
                    f"base state and reapply the prerequisite "
                    f"before retrying.")
            else:
                warnings_lines.append(
                    f"[{path}] [PAIR_FAILED] {kind} pair "
                    f"(old={oi}, new={ni}, scope={scope}): {reason}")

        for p in rec.patches:
            if p.get("applied_by") not in (
                    "failed", "skipped_structural_damage"):
                continue
            if p.get("applied_by") == "skipped_structural_damage":
                first_line = _patch_first_line(p, limit=70)
                reason = p.get("failure_reason", "unknown")
                warnings_lines.append(
                    f"[{path}] [STRUCT_DAMAGE] idx={p['idx']} "
                    f"(first line: {first_line!r}): {reason}")
                continue
            if p.get("tool_bug"):
                first_line = _patch_first_line(p, limit=70)
                reason = p["tool_bug"]
                warnings_lines.append(
                    f"[{path}] [TOOL_BUG] idx={p['idx']} "
                    f"(first line: {first_line!r}): {reason} "
                    f"— this is a deeppeek internal error, not a "
                    f"body-shape failure; report the exception "
                    f"type and message to deeppeek")
                continue
            first_line = _patch_first_line(p, limit=70)
            reason = p.get("failure_reason", "unknown")
            warnings_lines.append(
                f"[{path}] [PATCH_FAILED] idx={p['idx']} "
                f"(first line: {first_line!r}): {reason}")
            _ctx = _patch_context(p)
            if _ctx:
                warnings_lines.append(
                    f"[{path}] [PATCH_FAILED_CONTEXT] idx={p['idx']}: "
                    f"{_ctx!r}")

        for p in rec.patches:
            if not p.get("filtered_duplicate_lines"):
                continue
            status = p.get("applied_by", "?")
            fallback = (" (fallback: applied unfiltered)"
                        if p.get("filter_fallback") else "")
            warnings_lines.append(
                f"[{path}] [FILTERED] idx={p['idx']} "
                f"filtered duplicate block(s); final status: "
                f"{status}{fallback}")

        if rec.is_deleted:
            warnings_lines.append(
                f"[{path}] [INFO] file deleted by conversation")
            continue

        # ---- sanity-rejected: preserve target, dump content to
        #      aux/rejected/, dump repair suggestion to low_trust/ ----
        if getattr(rec, "sanity_rejected", False):
            reason = rec.verification.get(
                "sanity_reason",
                "sanity gate rejected write (over-deletion, empty "
                "result, or unrelated content)")
            warnings_lines.append(
                f"[{path}] [SANITY] {reason}; target file preserved")

            if rec.content is not None:
                rejected_dir = aux_dir / "rejected"
                rejected_dir.mkdir(parents=True, exist_ok=True)
                rejected_target = rejected_dir / out_name
                rejected_target.parent.mkdir(
                    parents=True, exist_ok=True)
                rejected_content = rec.content
                if not rejected_content.endswith("\n"):
                    rejected_content += "\n"
                rejected_target.write_text(rejected_content)

            if getattr(rec, "repair_content", None):
                low_dir.mkdir(parents=True, exist_ok=True)
                repaired_name = f"{out_name}.repaired"
                repaired_path = low_dir / repaired_name
                repaired_path.parent.mkdir(
                    parents=True, exist_ok=True)
                repaired_path.write_text(rec.repair_content)
                closers = rec.verification.get(
                    "sanity_repair_closers", "?")
                warnings_lines.append(
                    f"[{path}] [REPAIR] suggestion written to "
                    f"{'deeppeek/low_trust/' if apply_to is not None else 'files_low_trust/'}"
                    f"{repaired_name} (+{closers} closer(s); "
                    f"review before adopting)")
                if verbose:
                    print(f"  [repair] {path}: suggestion at "
                          f"{repaired_path}", file=sys.stderr)

            skipped.append({
                "path": path,
                "state": rec.state,
                "reason": reason,
                "sanity_rejected": True,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (sanity): {reason}",
                      file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue

        # ---- reconciled-to-base: no net change ----------------------
        if (rec.verification.get("reconciled_to_base")
                and rec.preexisting_base):
            reason = ("reconciled to base: applied items were "
                      "excluded as misattributed or unplaceable; "
                      "no net change")
            warnings_lines.append(f"[{path}] [NO_CHANGE] {reason}")
            skipped.append({
                "path": path, "state": rec.state, "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (no change): {reason}",
                      file=sys.stderr)
            preexisting_skipped_count += 1
            continue

        if rec.state in ("PARTIAL_ONLY", "MISSING"):
            reason = f"no full print (state={rec.state})"
            warnings_lines.append(
                f"[{path}] [{rec.state}] {reason}; not written")
            skipped.append({
                "path": path,
                "state": rec.state,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} ({rec.state}): {reason}",
                      file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue

        if rec.content is None:
            reason = "content is None after patching"
            warnings_lines.append(
                f"[{path}] [ERROR] {reason}; not written")
            skipped.append({
                "path": path,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (no content): {reason}",
                      file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue

        preexisting_unmodified = (
            rec.preexisting_base
            and not any(
                p.get("applied_by") not in _NO_OP_STATUSES
                for p in rec.patches
            )
            and not any(
                r.get("applied_by") not in _NO_OP_STATUSES
                for r in rec.verification.get("pair_records", [])
            )
        )
        if preexisting_unmodified:
            reason = ("pre-existing file with no conversation "
                      "changes; left as-is")
            skipped.append({
                "path": path,
                "state": rec.state,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (preexisting): {reason}",
                      file=sys.stderr)
            preexisting_skipped_count += 1
            continue

        if out_name in written_out_names:
            prev_src = written_out_names[out_name]
            reason = (
                f"basename collision with {prev_src!r} "
                f"(layout={layout})"
            )
            warnings_lines.append(
                f"[{path}] [ERROR] {reason}; not written")
            skipped.append({
                "path": path,
                "state": rec.state,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (collision): {reason}",
                      file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue
        written_out_names[out_name] = path

        if trust_rank.get(rec.trust, 0) < min_rank:
            why = _trust_reason(rec)
            reason = (f"trust={rec.trust}{why} below "
                      f"--min-trust {min_trust}")
            warnings_lines.append(
                f"[{path}] [{rec.trust}]{why} {reason}")
            skipped.append({
                "path": path,
                "trust": rec.trust,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if rec.content is not None:
                low_dir.mkdir(parents=True, exist_ok=True)
                low_target = low_dir / out_name
                low_target.parent.mkdir(
                    parents=True, exist_ok=True)
                low_content = rec.content
                if not low_content.endswith("\n"):
                    low_content += "\n"
                low_target.write_text(low_content)
                actually_written_low.add(out_name)
                if apply_to is not None:
                    warnings_lines.append(
                        f"[{path}] [{rec.trust}] -> "
                        f"deeppeek/low_trust/")
                else:
                    warnings_lines.append(
                        f"[{path}] [{rec.trust}] -> "
                        f"files_low_trust/")
            if verbose:
                dest = ("deeppeek/low_trust/"
                        if apply_to is not None
                        else "files_low_trust/")
                print(f"  SKIPPED {path} ({rec.trust}){why}: "
                      f"{reason} -> {dest}", file=sys.stderr)
                for w in rec.warnings[:3]:
                    print(f"    warning: {w}", file=sys.stderr)
            exit_code = max(exit_code, 1)
            continue

        if rec.trust == "LOW":
            exit_code = max(exit_code, 1)

        # ---- success: write to target ----------------------------
        target = files_dir / out_name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = rec.content
        if not content.endswith("\n"):
            content += "\n"
        target.write_text(content)
        actually_written.add(out_name)

        try:
            rec._was_written = True
        except Exception:
            pass

        sha = hashlib.sha256(content.encode()).hexdigest()
        manifest_base = (apply_to if apply_to is not None
                         else out_dir)
        try:
            rel_path = str(target.relative_to(manifest_base))
        except ValueError:
            rel_path = str(target)
        written[path] = {
            "state": rec.state,
            "source_blocks": rec.source_blocks,
            "all_full_idxs": rec.all_full_idxs,
            "chosen_full_idx": rec.chosen_full_idx,
            "dedup_reason": rec.dedup_reason,
            "last_full_idx": rec.last_full_idx,
            "patches": [
                {
                    "idx": p["idx"],
                    "applied_by": p.get("applied_by", "?"),
                    **({"filtered_duplicate_lines": True}
                       if p.get("filtered_duplicate_lines") else {}),
                    **({"filter_fallback": True}
                       if p.get("filter_fallback") else {}),
                }
                for p in rec.patches
            ],
            "pairs": [
                {"old_idx": p["old_idx"], "new_idx": p["new_idx"],
                 "scope_hint": p["scope_hint"]}
                for p in rec.pairs
            ],
            "is_new": rec.is_new,
            "is_deleted": False,
            "trust": rec.trust,
            "preexisting_base": rec.preexisting_base,
            "output_path": rel_path,
            "line_count": content.count("\n"),
            "sha256": sha,
            "verification": rec.verification,
        }
        written_count += 1

    # ------------------------------------------------------------------
    #  Deletions
    # ------------------------------------------------------------------
    deleted_from_records = {
        p for p, r in records.items() if r.is_deleted
    }
    all_deleted = sorted(set(deleted_files) | deleted_from_records)

    deleted_applied: list = []
    deleted_missing: list = []
    if apply_to is not None:
        deleted_applied, deleted_missing = _apply_deletions(
            apply_to, all_deleted, warnings_lines)

    deleted_lines = [
        "# Files marked [DEL]/[DELETED]/[REMOVED] or `rm` in the "
        "conversation.",
        "# In standard mode this is informational only.",
        "# In --apply-to mode, the deleted-applied section was actually",
        "# removed from the target tree.",
        "",
        "[marked-deleted]",
    ]
    for name in all_deleted:
        deleted_lines.append(name)
    if apply_to is not None:
        deleted_lines.append("")
        deleted_lines.append("[deleted-applied]")
        for name in deleted_applied:
            deleted_lines.append(name)
        if deleted_missing:
            deleted_lines.append("")
            deleted_lines.append("[deleted-not-found]")
            for name in deleted_missing:
                deleted_lines.append(name)
    (reports_dir / "deleted.txt").write_text(
        "\n".join(deleted_lines) + "\n")

    # ------------------------------------------------------------------
    #  Cross-validate problems → warnings
    # ------------------------------------------------------------------
    for gp in cross_validate.get("problems", []):
        if gp["type"] == "missing_module":
            warnings_lines.append(
                f"[graph] {gp['from']} imports module "
                f"'{gp['module']}' — not recovered")
        elif gp["type"] == "missing_export":
            warnings_lines.append(
                f"[graph] {gp['from']} imports '{gp['name']}' from "
                f"'{gp['target']}' — not exported")
    if cross_validate.get("problems"):
        exit_code = max(exit_code, 4)

    # ------------------------------------------------------------------
    #  Unattributed orphans
    # ------------------------------------------------------------------
    unattributed = [
        b for b in blocks
        if b.is_orphan and not b.attributed_to
        and b.attributed_by not in (
            "empty", "no_candidates", "skipped_meta",
            "not_a_file", "blockquote_skip",
        )
    ]
    for b in unattributed:
        first_line = _orphan_first_line(b)
        warnings_lines.append(
            f"[orphan] idx={b.idx} "
            f"(lines {b.line_start}..{b.line_end}) "
            f"could not be attributed to any file "
            f"(lang={b.fence_lang}, {b.body_line_count} line(s), "
            f"first: {first_line!r})")

    # ------------------------------------------------------------------
    #  MISSING_FILES
    # ------------------------------------------------------------------
    mentioned = _scan_mentioned_files(blocks)

    recovered_exts = _recovered_extensions(records)
    if recovered_exts:
        mentioned = {
            name for name in mentioned
            if any(name.endswith(ext) for ext in recovered_exts)
        }

    imported_missing = _aggregate_missing_imports(records)
    recovered = {_basename(p) for p, r in records.items()
                 if r.content is not None}
    deleted_set = set(deleted_files)

    project_prefixes = _recovered_name_prefixes(records)
    if project_prefixes:
        mentioned = {
            name for name in mentioned
            if any(name.startswith(p) for p in project_prefixes)
        }
    else:
        mentioned = set()

    # on_disk: use the caller-supplied set when available (pipeline
    # passes it from `scan_apply_to`), else fall back to the old
    # `rglob` walk.  The fallback is for external callers (tests) who
    # don't have the state-threaded form.
    if on_disk_names is not None:
        on_disk: set = set(on_disk_names)
    else:
        on_disk = set()
        if apply_to is not None and apply_to.is_dir():
            for p in apply_to.rglob("*"):
                if p.is_file():
                    on_disk.add(p.name)

    missing = (
        (mentioned | imported_missing) - recovered - deleted_set
        - SKIP_MISSING_FILES - on_disk
    )

    if missing:
        lines = [
            "The following source files are referenced in the "
            "transcript",
            "(either named in prose, or imported by a recovered file)",
            "but were never printed as recoverable code blocks:",
            "",
        ]
        for name in sorted(missing):
            lines.append(f"## {name}")
            lines.append("")
        (reports_dir / "MISSING_FILES.txt").write_text(
            "\n".join(lines) + "\n")
        warnings_lines.append(
            f"[missing] {len(missing)} file(s) referenced but not "
            f"recovered — see MISSING_FILES.txt")
        exit_code = max(exit_code, 2)
    else:
        (reports_dir / "MISSING_FILES.txt").write_text(
            "# No missing files.\n"
            "# This file is written on every --apply-to run.  An "
            "empty or all-comment body means the transcript\n"
            "# did not reference any source file that failed to "
            "recover.\n")

    # ------------------------------------------------------------------
    #  Pre-existing file tracking
    # ------------------------------------------------------------------
    untouched_files = pre_existing_files - actually_written
    untouched_low = pre_existing_low - actually_written_low
    overwritten_files = pre_existing_files & actually_written
    overwritten_low = pre_existing_low & actually_written_low

    if untouched_files or untouched_low:
        total = len(untouched_files) + len(untouched_low)
        warnings_lines.append(
            f"[preserved] {total} pre-existing file(s) in the output "
            f"directory left untouched (not part of this run)")
        if apply_to is None:
            for name in sorted(untouched_files):
                warnings_lines.append(f"  files/{name}")
            for name in sorted(untouched_low):
                warnings_lines.append(
                    f"  files_low_trust/{name}")
        else:
            warnings_lines.append(
                "  (per-file listing suppressed in --apply-to mode; "
                "the target tree owns its own files)")

    if preexisting_skipped_count:
        warnings_lines.append(
            f"[preexisting] {preexisting_skipped_count} pre-existing "
            f"file(s) left unchanged (not part of this run)")

    if cross_file_duplicates:
        for _d in cross_file_duplicates:
            _files = ", ".join(_d["files"])
            warnings_lines.append(
                f"[CROSS_FILE_DUP] {_d['kind']} "
                f"{_d['name']!r} ({_d['line_count']} lines) "
                f"newly-added to: {_files}")

    if overwritten_files or overwritten_low:
        warnings_lines.append(
            "[overwritten] pre-existing files overwritten by this "
            "run:")
        for name in sorted(overwritten_files):
            warnings_lines.append(f"  {name}")
        for name in sorted(overwritten_low):
            warnings_lines.append(
                f"  deeppeek/low_trust/{name}")

    # ------------------------------------------------------------------
    #  repairs.json
    # ------------------------------------------------------------------
    repairs_manifest = {}
    for rec_path, rec in records.items():
        if not getattr(rec, "repair_content", None):
            continue
        rec_out_name = (
            os.path.basename(rec_path) if layout == "flat"
            else rec_path
        )
        repairs_manifest[rec_path] = {
            "failure_reason": rec.verification.get("sanity_reason"),
            "closers_added": rec.verification.get(
                "sanity_repair_closers"),
            "suggested_file": f"{rec_out_name}.repaired",
        }
    (reports_dir / "repairs.json").write_text(
        json.dumps(repairs_manifest, indent=2))

    # ------------------------------------------------------------------
    #  warnings.txt (aggregated)
    # ------------------------------------------------------------------
    warnings_lines = _aggregate_warnings(warnings_lines)
    warnings_text = "\n".join(warnings_lines)
    if warnings_text:
        warnings_text += "\n"
    (reports_dir / "warnings.txt").write_text(warnings_text)

    # ------------------------------------------------------------------
    #  Escalations, block-classification counts, decisions
    # ------------------------------------------------------------------
    escalations = []
    for b in blocks:
        if b.escalation_reason is None:
            continue
        escalations.append({
            "idx": b.idx,
            "path": b.canonical_path,
            "fence_lang": b.fence_lang,
            "reason": b.escalation_reason,
            "heuristic_kind": b.heuristic_kind,
            "llm_kind": b.llm_kind,
            "final_kind": b.kind,
            "override_reason": b.override_reason,
            "classified_by": b.classified_by,
        })

    by_heuristic = sum(
        1 for b in blocks if b.classified_by == "heuristic")
    by_llm = sum(1 for b in blocks if b.classified_by == "llm")
    by_preexisting = sum(
        1 for b in blocks if b.classified_by == "preexisting")
    escalated = sum(
        1 for b in blocks if b.escalation_reason is not None)
    overridden = sum(
        1 for b in blocks if b.override_reason is not None)
    orphans_attributed = sum(
        1 for b in blocks if b.is_orphan and b.attributed_to)

    decisions = {}
    for path, rec in records.items():
        patch_idxs = [p["idx"] for p in rec.patches]
        source_idxs = list(rec.source_blocks)
        for pidx in patch_idxs:
            if pidx not in source_idxs:
                source_idxs.append(pidx)

        pair_records = rec.verification.get("pair_records", [])
        pair_failures = []
        state_drift_count = 0
        for r in pair_records:
            if r.get("applied_by") == "failed":
                pair = r.get("pair", {})
                reason = r.get("reason", "unknown")
                if ("not found in base" in reason
                        or "state drift" in reason):
                    state_drift_count += 1
                pair_failures.append({
                    "old_idx": pair.get("old_idx"),
                    "new_idx": pair.get("new_idx"),
                    "scope_hint": pair.get("scope_hint"),
                    "reason": reason,
                })
        sanity_reason = (
            rec.verification.get("sanity_reason")
            if getattr(rec, "sanity_rejected", False) else None
        )

        patch_failure_reasons = [
            {
                "idx": p["idx"],
                "first_line": _patch_first_line(p, limit=70),
                "context_before": _patch_context(p, limit=800),
                "reason": p.get("failure_reason", "unknown"),
                **({"tool_bug": p["tool_bug"]}
                   if p.get("tool_bug") else {}),
            }
            for p in rec.patches
            if p.get("applied_by") in _PLACEMENT_FAILURES
        ]

        decisions[path] = {
            "state": rec.state,
            "source_block_idxs": source_idxs,
            "chosen_full_idx": rec.chosen_full_idx,
            "chosen_by": rec.dedup_reason,
            "trust": rec.trust,
            "preexisting_base": rec.preexisting_base,
            "sanity_rejected": getattr(rec, "sanity_rejected", False),
            "sanity_reason": sanity_reason,
            "written": path in written,
            "patch_count": len(rec.patches),
            "pair_count": len(rec.pairs),
            "patch_failures": [
                p["idx"] for p in rec.patches
                if p.get("applied_by") in _PLACEMENT_FAILURES
            ],
            "patch_failure_reasons": patch_failure_reasons,
            "pair_failures": pair_failures,
            "state_drift_count": state_drift_count,
            "verification": rec.verification,
            "warnings": rec.warnings,
        }

    # ------------------------------------------------------------------
    #  Manifest + summary
    # ------------------------------------------------------------------
    state = {
        "apply_to": apply_to,
        "layout": layout,
        "files_dir": files_dir,
        "reports_dir": reports_dir,
        "dump_display": dump_display,
        "dump_size": dump_size,
        "dump_sha": dump_sha,
        "llm": llm,
        "llm_mode": llm_mode,
        "run_errors": _run_errors,
        "pass_timings": pass_timings,
        "blocks": blocks,
        "records": records,
        "escalations": escalations,
        "by_heuristic": by_heuristic,
        "by_llm": by_llm,
        "by_preexisting": by_preexisting,
        "escalated": escalated,
        "overridden": overridden,
        "orphans_attributed": orphans_attributed,
        "unattributed": unattributed,
        "written": written,
        "written_count": written_count,
        "skipped": skipped,
        "decisions": decisions,
        "cross_validate": cross_validate,
        "resolutions": resolutions,
        "cross_file_duplicates": cross_file_duplicates,
        "missing": missing,
        "all_deleted": all_deleted,
        "deleted_applied": deleted_applied,
        "deleted_missing": deleted_missing,
        "preexisting_skipped_count": preexisting_skipped_count,
        "untouched_files": untouched_files,
        "untouched_low": untouched_low,
        "overwritten_files": overwritten_files,
        "overwritten_low": overwritten_low,
        "total_block_count": len(blocks),
    }

    manifest = build_manifest(state)
    (reports_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2))

    print_summary(state)

    return exit_code
