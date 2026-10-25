"""
recover.write — Pass 9 (write files, manifest, warnings).

New in 2026-11-01: `sanity_rejected` branch.  A file marked with
`sanity_rejected = True` by `apply_patches` is treated exactly like
a `LOW`-trust file: not written to the target tree, reported in
`warnings.txt`, and preserved as-is.

New in 2026-11-01 (b): trust reason names the specific check
(`patches_incomplete`, `pairs_incomplete`, `ast_parse_failed`).
Unattributed orphans report their first content line.

New in 2026-11-01 (c): `patch_failure_reasons` in `decisions[path]`.
Each failed patch records its `idx`, `first_line`, and `reason`.
`warnings.txt` gets a `[PATCH_FAILED]` line per failed patch.  The
final summary prints one PATCH_FAILED / PAIR_FAILED line per failure
so the user sees the diagnostics in the console without opening
files.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .core import (
    Block, FileRecord,
    SKIP_MISSING_FILES, SOURCE_EXTENSIONS,
    _basename,
)
from .llm import LLMClient
from .validate import _py_imports, _is_likely_local


def _scan_mentioned_files(dump_text: str) -> set:
    mentioned: set = set()
    for m in re.finditer(r'[A-Za-z0-9_./\-]+', dump_text):
        tok = m.group(0)
        if any(tok.endswith(ext) for ext in SOURCE_EXTENSIONS):
            base = os.path.basename(tok)
            if base and len(base) > 3:
                mentioned.add(base)
    return mentioned


def _aggregate_missing_imports(records: dict) -> set:
    expected: set = set()
    for path, rec in records.items():
        if not path.endswith(".py"):
            continue
        if rec.content is None:
            continue
        for module, _name in _py_imports(rec.content):
            if _is_likely_local(module, records):
                clean = module.lstrip(".")
                if not clean:
                    continue
                last = clean.rsplit(".", 1)[-1]
                expected.add(_basename(last + ".py"))
    recovered = {_basename(p) for p, r in records.items()
                 if r.content is not None}
    return expected - recovered


def _recovered_name_prefixes(records: dict) -> set:
    prefixes: set = set()
    for path, rec in records.items():
        if rec.content is None:
            continue
        base = _basename(path)
        if not base or "." not in base:
            continue
        stem = base.rsplit(".", 1)[0]
        if "_" not in stem:
            continue
        prefix = stem.split("_", 1)[0] + "_"
        if len(prefix) > 2:
            prefixes.add(prefix)
    return prefixes


def _recovered_extensions(records: dict) -> set:
    exts: set = set()
    for path, rec in records.items():
        if rec.content is None:
            continue
        ext = Path(path).suffix.lower()
        if ext:
            exts.add(ext)
    return exts


def _apply_deletions(apply_to: Path,
                     all_deleted: list,
                     warnings_lines: list) -> tuple:
    applied: list = []
    missing: list = []
    for name in all_deleted:
        base = os.path.basename(name)
        candidates = []
        if base and base != name:
            candidates.append(apply_to / base)
        candidates.append(apply_to / name)

        target = None
        for c in candidates:
            if c.exists() and c.is_file():
                target = c
                break

        if target is None and base:
            matches = [
                p for p in apply_to.rglob(base)
                if p.is_file() and p.name == base
                and "deeppeek" not in p.parts
                and ".deeppeek" not in p.parts
            ]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                rel_matches = [
                    m.relative_to(apply_to).as_posix() for m in matches
                ]
                warnings_lines.append(
                    f"[{name}] [AMBIGUOUS] {len(matches)} files match "
                    f"basename ({rel_matches}); not deleted")
                missing.append(name)
                continue

        if target is None:
            missing.append(name)
            continue

        try:
            target.unlink()
            applied.append(name)
            warnings_lines.append(
                f"[{name}] [DELETED] removed from target tree")
        except Exception as e:
            warnings_lines.append(
                f"[{name}] [ERROR] could not delete: {e}")
            missing.append(name)
    return applied, missing


def _trust_reason(rec: FileRecord) -> str:
    """Human-readable string listing which check(s) triggered LOW trust."""
    reasons = []
    if rec.verification.get("patches_incomplete"):
        reasons.append("patches_incomplete")
    if rec.verification.get("pairs_incomplete"):
        reasons.append("pairs_incomplete")
    ast_check = rec.verification.get("ast_parse", "")
    if isinstance(ast_check, str) and ast_check.startswith("FAIL"):
        reasons.append("ast_parse_failed")
    if reasons:
        return " [" + ", ".join(reasons) + "]"
    return ""


def _orphan_first_line(block: Block) -> str:
    """First non-empty body line of an orphan block, truncated."""
    if not block.body:
        return "(empty)"
    for ln in block.body.splitlines():
        s = ln.strip()
        if s:
            if len(s) > 60:
                s = s[:57] + "..."
            return s
    return "(empty)"


def _patch_first_line(patch: dict, limit: int = 60) -> str:
    """First non-empty line of a patch's body, truncated."""
    body = patch.get("body", "") or ""
    for ln in body.splitlines():
        s = ln.strip()
        if s:
            return s[:limit]
    return ""


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
                verbose: bool = False) -> int:
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
    written: dict = {}
    skipped: list = []
    written_count = 0
    exit_code = 0
    written_out_names: dict = {}
    actually_written: set = set()
    actually_written_low: set = set()
    preexisting_skipped_count = 0

    for path, rec in sorted(records.items()):
        if layout == "flat":
            out_name = os.path.basename(path)
        else:
            out_name = path

        for w in rec.warnings:
            warnings_lines.append(f"[{path}] [WARN] {w}")

        # Per-pair failure detail: name which pair(s) failed and why.
        for r in rec.verification.get("pair_records", []):
            if r.get("applied_by") != "failed":
                continue
            pair = r.get("pair", {})
            oi = pair.get("old_idx")
            ni = pair.get("new_idx")
            scope = pair.get("scope_hint", "replace")
            reason = r.get("reason", "unknown")
            kind = "scoped" if oi == -1 else "replace"
            warnings_lines.append(
                f"[{path}] [PAIR_FAILED] {kind} pair "
                f"(old={oi}, new={ni}, scope={scope}): {reason}")

        # Per-patch failure detail.
        for p in rec.patches:
            if p.get("applied_by") != "failed":
                continue
            first_line = _patch_first_line(p, limit=70)
            reason = p.get("failure_reason", "unknown")
            warnings_lines.append(
                f"[{path}] [PATCH_FAILED] idx={p['idx']} "
                f"(first line: {first_line!r}): {reason}")

        if rec.is_deleted:
            warnings_lines.append(
                f"[{path}] [INFO] file deleted by conversation")
            continue

        if getattr(rec, "sanity_rejected", False):
            reason = rec.verification.get(
                "sanity_reason",
                "sanity gate rejected write (over-deletion, empty result, "
                "or unrelated content)")
            warnings_lines.append(
                f"[{path}] [SANITY] {reason}; target file preserved")
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

        # "No conversation changes" means: no patch and no pair was
        # attempted with any effect.  A "failed" patch is an attempt
        # that could not land — it is NOT a no-op.  Treating it as
        # "preexisting unmodified" would silently hide the failure
        # before the trust check could route it to low_trust/.
        #
        # "already-present" patches ARE no-ops: the content is
        # already in the file, so the target needs no write.
        preexisting_unmodified = (
            rec.preexisting_base
            and not rec.pairs
            and not any(
                p.get("applied_by") != "already-present"
                for p in rec.patches
            )
        )
        if preexisting_unmodified:
            reason = ("pre-existing file with no conversation "
                      "changes; left as-is")
            warnings_lines.append(f"[{path}] [PREEXISTING] {reason}")
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
            reason = f"trust={rec.trust}{why} below --min-trust {min_trust}"
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
                low_target.parent.mkdir(parents=True, exist_ok=True)
                low_content = rec.content
                if not low_content.endswith("\n"):
                    low_content += "\n"
                low_target.write_text(low_content)
                actually_written_low.add(out_name)
                if apply_to is not None:
                    warnings_lines.append(
                        f"[{path}] [{rec.trust}] -> deeppeek/low_trust/")
                else:
                    warnings_lines.append(
                        f"[{path}] [{rec.trust}] -> files_low_trust/")
            if verbose:
                dest = ("deeppeek/low_trust/" if apply_to is not None
                        else "files_low_trust/")
                print(f"  SKIPPED {path} ({rec.trust}){why}: {reason} "
                      f"-> {dest}", file=sys.stderr)
                for w in rec.warnings[:3]:
                    print(f"    warning: {w}", file=sys.stderr)
            exit_code = max(exit_code, 1)
            continue

        if rec.trust == "LOW":
            exit_code = max(exit_code, 1)

        target = files_dir / out_name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = rec.content
        if not content.endswith("\n"):
            content += "\n"
        target.write_text(content)
        actually_written.add(out_name)

        sha = hashlib.sha256(content.encode()).hexdigest()
        manifest_base = apply_to if apply_to is not None else out_dir
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
                {"idx": p["idx"], "applied_by": p.get("applied_by", "?")}
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

    deleted_from_records = {p for p, r in records.items() if r.is_deleted}
    all_deleted = sorted(set(deleted_files) | deleted_from_records)

    deleted_applied: list = []
    deleted_missing: list = []
    if apply_to is not None:
        deleted_applied, deleted_missing = _apply_deletions(
            apply_to, all_deleted, warnings_lines)

    deleted_lines = [
        "# Files marked [DEL]/[DELETED]/[REMOVED] or `rm` in the conversation.",
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

    unattributed = [b for b in blocks
                    if b.is_orphan and not b.attributed_to
                    and b.attributed_by not in ("empty", "no_candidates",
                                                 "skipped_meta")]
    for b in unattributed:
        first_line = _orphan_first_line(b)
        warnings_lines.append(
            f"[orphan] idx={b.idx} (lines {b.line_start}..{b.line_end}) "
            f"could not be attributed to any file "
            f"(lang={b.fence_lang}, {b.body_line_count} line(s), "
            f"first: {first_line!r})")

    mentioned = _scan_mentioned_files(dump_text)

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

    missing = (
        (mentioned | imported_missing) - recovered - deleted_set
        - SKIP_MISSING_FILES
    )

    if missing:
        lines = [
            "The following source files are referenced in the transcript",
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
            f"[missing] {len(missing)} file(s) referenced but not recovered "
            f"— see MISSING_FILES.txt")
        exit_code = max(exit_code, 2)
    else:
        (reports_dir / "MISSING_FILES.txt").write_text(
            "# No missing files.\n"
            "# This file is written on every --apply-to run.  An "
            "empty or all-comment body means the transcript\n"
            "# did not reference any source file that failed to "
            "recover.\n")

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
                warnings_lines.append(f"  files_low_trust/{name}")
        else:
            warnings_lines.append(
                "  (per-file listing suppressed in --apply-to mode; "
                "the target tree owns its own files)")

    if overwritten_files or overwritten_low:
        warnings_lines.append(
            "[overwritten] pre-existing files overwritten by this run:")
        for name in sorted(overwritten_files):
            warnings_lines.append(f"  {name}")
        for name in sorted(overwritten_low):
            warnings_lines.append(f"  deeppeek/low_trust/{name}")

    warnings_text = "\n".join(warnings_lines)
    if warnings_text:
        warnings_text += "\n"
    (reports_dir / "warnings.txt").write_text(warnings_text)

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

    by_heuristic = sum(1 for b in blocks if b.classified_by == "heuristic")
    by_llm = sum(1 for b in blocks if b.classified_by == "llm")
    by_preexisting = sum(1 for b in blocks if b.classified_by == "preexisting")
    escalated = sum(1 for b in blocks if b.escalation_reason is not None)
    overridden = sum(1 for b in blocks if b.override_reason is not None)
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
        for r in pair_records:
            if r.get("applied_by") == "failed":
                pair = r.get("pair", {})
                pair_failures.append({
                    "old_idx": pair.get("old_idx"),
                    "new_idx": pair.get("new_idx"),
                    "scope_hint": pair.get("scope_hint"),
                    "reason": r.get("reason", "unknown"),
                })
        sanity_reason = (
            rec.verification.get("sanity_reason")
            if getattr(rec, "sanity_rejected", False) else None
        )

        patch_failure_reasons = [
            {
                "idx": p["idx"],
                "first_line": _patch_first_line(p, limit=70),
                "reason": p.get("failure_reason", "unknown"),
            }
            for p in rec.patches
            if p.get("applied_by") == "failed"
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
                if p.get("applied_by") == "failed"
            ],
            "patch_failure_reasons": patch_failure_reasons,
            "pair_failures": pair_failures,
            "verification": rec.verification,
            "warnings": rec.warnings,
        }

    manifest = {
        "dump": {
            "path": dump_display,
            "sha256": dump_sha,
            "byte_len": dump_size,
        },
        "run": {
            "timestamp": datetime.now(timezone.utc)
                          .isoformat(timespec="seconds"),
            "mode": "apply" if apply_to is not None else "recover",
            "apply_to": str(apply_to) if apply_to is not None else None,
            "layout": layout,
            "llm_mode": llm_mode,
            "llm_model": llm.model if llm else None,
            "llm_calls": {
                "live": llm.calls_made if llm else 0,
                "cached": llm.calls_cached if llm else 0,
                "refused": llm.calls_refused if llm else 0,
            },
            "block_classification": {
                "total": len(blocks),
                "escalated_to_llm": escalated,
                "overridden_by_hard_rules": overridden,
                "classified_by_heuristic": by_heuristic,
                "classified_by_llm": by_llm,
                "classified_by_preexisting": by_preexisting,
                "orphans_attributed": orphans_attributed,
            },
            "escalations": escalations,
            "resolutions": resolutions or [],
            "cross_validate": cross_validate,
            "missing_files": sorted(missing) if missing else [],
            "deleted_applied": deleted_applied,
            "deleted_not_found": deleted_missing,
            "preexisting_unchanged": preexisting_skipped_count,
        },
        "files": written,
        "skipped": skipped,
        "decisions": decisions,
        "deleted": all_deleted,
    }
    (reports_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    if skipped:
        by_reason: dict = {
            "no_full":     [],
            "low_trust":   [],
            "sanity":      [],
            "no_content":  [],
            "collision":   [],
            "preexisting": [],
            "other":       [],
        }
        for s in skipped:
            r = s.get("reason", "")
            if "no full print" in r:
                by_reason["no_full"].append(s["path"])
            elif "sanity gate rejected" in r or "truncation" in r:
                by_reason["sanity"].append(s["path"])
            elif "trust=" in r:
                by_reason["low_trust"].append(s["path"])
            elif "content is None" in r:
                by_reason["no_content"].append(s["path"])
            elif "basename collision" in r:
                by_reason["collision"].append(s["path"])
            elif "pre-existing" in r:
                by_reason["preexisting"].append(s["path"])
            else:
                by_reason["other"].append(s["path"])

        print(f"\n  Skipped {len(skipped)} file(s):", file=sys.stderr)
        for label, paths in by_reason.items():
            if not paths:
                continue
            head = ", ".join(paths[:5])
            tail = f" … (+{len(paths)-5} more)" if len(paths) > 5 else ""
            print(f"    {label}: {len(paths)}  ({head}{tail})",
                  file=sys.stderr)
        print(f"    Full list: {reports_dir / 'warnings.txt'}",
              file=sys.stderr)

    xv = cross_validate.get("problems", [])
    if xv:
        missing_mods = sorted({
            p["module"] for p in xv if p["type"] == "missing_module"
        })
        if missing_mods:
            print(f"  missing modules ({len(missing_mods)}): "
                  f"{', '.join(missing_mods)}", file=sys.stderr)

    if apply_to is not None:
        print(f"\nApplied {written_count} file(s) to {files_dir}/")
        print(f"  reports: {reports_dir}/")
    else:
        print(f"\nWrote {written_count} files to {files_dir}/")
    print(f"  skipped:  {len(skipped)}"
          + ("  (see warnings.txt)" if skipped else ""))

    fail_closed_count = sum(
        1 for rec in records.values()
        if rec.trust == "LOW"
        and not rec.is_deleted
        and not getattr(rec, "sanity_rejected", False)
        and rec.content is not None
    )
    if fail_closed_count:
        print(f"  FAIL-CLOSED: {fail_closed_count} file(s) with "
              f"incomplete application — target preserved, content in "
              f"{'deeppeek/low_trust/' if apply_to else 'files_low_trust/'}",
              file=sys.stderr)
        print(f"    (use --min-trust LOW to force-write them)",
              file=sys.stderr)
        # Per-failure detail in the summary.
        for fpath, frec in sorted(records.items()):
            if frec.trust != "LOW":
                continue
            if frec.is_deleted or getattr(frec, "sanity_rejected", False):
                continue
            if frec.content is None:
                continue
            for pf in frec.patches:
                if pf.get("applied_by") != "failed":
                    continue
                first_line = _patch_first_line(pf, limit=60)
                reason = pf.get("failure_reason", "unknown")
                print(f"    PATCH_FAILED {fpath} idx={pf['idx']}: "
                      f"{first_line!r} — {reason}",
                      file=sys.stderr)
            for r in frec.verification.get("pair_records", []):
                if r.get("applied_by") != "failed":
                    continue
                pair = r.get("pair", {})
                oi = pair.get("old_idx")
                reason = r.get("reason", "unknown")
                print(f"    PAIR_FAILED {fpath} (old={oi}): {reason}",
                      file=sys.stderr)

    if preexisting_skipped_count:
        print(f"  preexisting: {preexisting_skipped_count} file(s) left "
              f"as-is (unchanged by the conversation)")

    if deleted_applied:
        print(f"  deleted:  {len(deleted_applied)} file(s) removed "
              f"from target tree")
        for name in deleted_applied[:5]:
            print(f"    {name}")
        if len(deleted_applied) > 5:
            print(f"    ... +{len(deleted_applied) - 5} more")
    if deleted_missing and apply_to is not None:
        print(f"  del-miss: {len(deleted_missing)} file(s) marked "
              f"[DEL] but not present in target tree")

    total_preserved = len(untouched_files) + len(untouched_low)
    if total_preserved:
        print(f"  preserved: {total_preserved} pre-existing file(s) "
              f"left untouched (not part of this run)")
        if apply_to is None:
            shown = 0
            for name in sorted(untouched_files)[:5]:
                print(f"    files/{name}")
                shown += 1
            for name in sorted(untouched_low)[:5]:
                print(f"    files_low_trust/{name}")
                shown += 1
            if total_preserved > shown:
                print(f"    ... +{total_preserved - shown} more "
                      f"(see warnings.txt)")
    total_overwritten = len(overwritten_files) + len(overwritten_low)
    if total_overwritten:
        print(f"  overwrote: {total_overwritten} pre-existing file(s) "
              f"that shared a name with this run's output")

    print(f"  classified: {by_heuristic} heuristic / {by_llm} LLM"
          + (f" / {by_preexisting} preexisting" if by_preexisting else "")
          + f" (escalated {escalated}/{len(blocks)}, "
          f"{overridden} overridden by hard rules)")
    print(f"  orphans:  {orphans_attributed} attributed to a file"
          + (f", {len(unattributed)} unattributed (see warnings.txt)"
             if unattributed else ""))
    print(f"  graph:    {len(xv)} import problem(s)"
          if xv else "  graph:    clean")
    print(f"  deleted:  {reports_dir / 'deleted.txt'} "
          f"({len(all_deleted)} marked"
          + (f", {len(deleted_applied)} applied)" if apply_to is not None
             else f")"))
    if missing:
        print(f"  missing:  {reports_dir / 'MISSING_FILES.txt'} "
              f"({len(missing)} files referenced but never recovered)")
    if llm is not None:
        print(f"  LLM:      {llm.calls_made} live, "
              f"{llm.calls_cached} cached, "
              f"{llm.calls_refused} refused")
    print(f"  warnings: {reports_dir / 'warnings.txt'}")
    print(f"  manifest: {reports_dir / 'manifest.json'}")
    return exit_code
