"""
recover.write — Pass 9 (write files, manifest, warnings).

DIFFICULT.  Owns:

    FIX [D]   --min-trust-filtered files are written to a shadow
              `files_low_trust/` directory instead of being silently
              dropped.  Content is preserved for inspection even when
              the primary write path excludes it.

    FIX [6]   Per-file skip detail is gated behind --trace (verbose).
              Default output is a category summary.

    FIX [7]   Category summary at end: "Skipped N files: X no_full,
              Y low_trust, Z no_content".

    FIX [8]   New manifest.decisions section: `jq '.decisions["foo.py"]'`
              answers "why is this file missing" without grepping
              warnings.txt.

    FIX [E]   MISSING_FILES.txt is filtered in two ways before writing:

                1. Extension filter — only extensions the recovered
                   project actually uses are considered.

                2. Prefix filter — only files whose stem shares a
                   `_`-separated prefix with a recovered file are
                   listed.  If no prefix structure exists at all,
                   prose-only mentions are dropped entirely.

    NEW (2026-10-21)  Basename-collision detection for the flat layout.

    NEW (2026-10-21b)  "missing modules" summary line.

    NEW (2026-10-22)  Pre-existing file preservation report.

    NEW (2026-10-23)  Manifest accepts dump_display/dump_size so the
              pipeline can be fed by an in-memory string (from
              `deeppeek capture`'s default mode) — dump_display is
              recorded as "<in-memory>" in that case.
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


def _scan_mentioned_files(dump_text: str) -> set[str]:
    mentioned: set[str] = set()
    for m in re.finditer(r'[A-Za-z0-9_./\-]+', dump_text):
        tok = m.group(0)
        if any(tok.endswith(ext) for ext in SOURCE_EXTENSIONS):
            base = os.path.basename(tok)
            if base and len(base) > 3:
                mentioned.add(base)
    return mentioned


def _aggregate_missing_imports(records: dict[str, FileRecord]) -> set[str]:
    """Collect filenames imported by recovered .py files that are not
    themselves recovered."""
    expected: set[str] = set()
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


def _recovered_name_prefixes(records: dict[str, FileRecord]) -> set[str]:
    """Filename-stem prefixes shared by the recovered files."""
    prefixes: set[str] = set()
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


def _recovered_extensions(records: dict[str, FileRecord]) -> set[str]:
    """Extensions present in the recovered file set."""
    exts: set[str] = set()
    for path, rec in records.items():
        if rec.content is None:
            continue
        ext = Path(path).suffix.lower()
        if ext:
            exts.add(ext)
    return exts


def pass9_write(records: dict[str, FileRecord],
                blocks: list[Block],
                deleted_files: list[str],
                out_dir: Path,
                layout: str,
                remap: dict[str, str],
                min_trust: str,
                dump_display: str,
                dump_size: int,
                dump_sha: str,
                dump_text: str,
                llm: Optional[LLMClient],
                llm_mode: str,
                cross_validate: dict,
                verbose: bool = False) -> int:
    files_dir = out_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "llm_audit").mkdir(parents=True, exist_ok=True)

    pre_existing_files: set[str] = (
        {p.name for p in files_dir.iterdir() if p.is_file()}
        if files_dir.exists() else set()
    )
    _low_dir = out_dir / "files_low_trust"
    pre_existing_low: set[str] = (
        {p.name for p in _low_dir.iterdir() if p.is_file()}
        if _low_dir.exists() else set()
    )

    trust_rank = {"SKIP": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
    min_rank = trust_rank[min_trust]

    warnings_lines: list[str] = []
    written: dict[str, dict] = {}
    skipped: list[dict] = []
    written_count = 0
    exit_code = 0
    written_out_names: dict[str, str] = {}
    actually_written: set[str] = set()
    actually_written_low: set[str] = set()

    for path, rec in sorted(records.items()):
        if layout == "flat":
            out_name = os.path.basename(path)
        elif layout == "header":
            out_name = path
        else:
            out_name = path
            for old, new in remap.items():
                if out_name.startswith(old):
                    out_name = new + out_name[len(old):]
                    break

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

        if rec.is_deleted:
            warnings_lines.append(
                f"[{path}] [INFO] file deleted by conversation")
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

        if trust_rank.get(rec.trust, 0) < min_rank:
            reason = f"trust={rec.trust} below --min-trust {min_trust}"
            warnings_lines.append(
                f"[{path}] [{rec.trust}] {reason}")
            skipped.append({
                "path": path,
                "trust": rec.trust,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if rec.content is not None:
                low_dir = out_dir / "files_low_trust"
                low_dir.mkdir(parents=True, exist_ok=True)
                low_target = low_dir / out_name
                low_target.parent.mkdir(parents=True, exist_ok=True)
                low_content = rec.content
                if not low_content.endswith("\n"):
                    low_content += "\n"
                low_target.write_text(low_content)
                actually_written_low.add(out_name)
                warnings_lines.append(
                    f"[{path}] [{rec.trust}] -> files_low_trust/")
            if verbose:
                print(f"  SKIPPED {path} ({rec.trust}): {reason} "
                      f"-> files_low_trust/", file=sys.stderr)
                for w in rec.warnings[:3]:
                    print(f"    warning: {w}", file=sys.stderr)
            exit_code = max(exit_code, 1)
            continue

        if rec.trust == "LOW":
            exit_code = max(exit_code, 1)

        for w in rec.warnings:
            warnings_lines.append(f"[{path}] [WARN] {w}")

        target = files_dir / out_name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = rec.content
        if not content.endswith("\n"):
            content += "\n"
        target.write_text(content)
        actually_written.add(out_name)

        sha = hashlib.sha256(content.encode()).hexdigest()
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
            "is_new": rec.is_new,
            "is_deleted": False,
            "trust": rec.trust,
            "output_path": str(target.relative_to(out_dir)),
            "line_count": content.count("\n"),
            "sha256": sha,
            "verification": rec.verification,
        }
        written_count += 1

    deleted_from_records = {p for p, r in records.items() if r.is_deleted}
    all_deleted = sorted(set(deleted_files) | deleted_from_records)
    deleted_text = "\n".join(all_deleted)
    if deleted_text:
        deleted_text += "\n"
    (out_dir / "deleted.txt").write_text(deleted_text)

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
        warnings_lines.append(
            f"[orphan] idx={b.idx} (lines {b.line_start}..{b.line_end}) "
            f"could not be attributed to any file "
            f"(lang={b.fence_lang}, {b.body_line_count} lines)")

    # ---- MISSING_FILES.txt ----
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
        (out_dir / "MISSING_FILES.txt").write_text("\n".join(lines) + "\n")
        warnings_lines.append(
            f"[missing] {len(missing)} file(s) referenced but not recovered "
            f"— see MISSING_FILES.txt")
        exit_code = max(exit_code, 2)

    untouched_files = pre_existing_files - actually_written
    untouched_low = pre_existing_low - actually_written_low
    overwritten_files = pre_existing_files & actually_written
    overwritten_low = pre_existing_low & actually_written_low

    if untouched_files or untouched_low:
        warnings_lines.append(
            "[preserved] pre-existing files in output directory left "
            "untouched (not produced by this run):")
        for name in sorted(untouched_files):
            warnings_lines.append(f"  files/{name}")
        for name in sorted(untouched_low):
            warnings_lines.append(f"  files_low_trust/{name}")

    if overwritten_files or overwritten_low:
        warnings_lines.append(
            "[overwritten] pre-existing files overwritten by this run:")
        for name in sorted(overwritten_files):
            warnings_lines.append(f"  files/{name}")
        for name in sorted(overwritten_low):
            warnings_lines.append(f"  files_low_trust/{name}")

    warnings_text = "\n".join(warnings_lines)
    if warnings_text:
        warnings_text += "\n"
    (out_dir / "warnings.txt").write_text(warnings_text)

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
    escalated = sum(1 for b in blocks if b.escalation_reason is not None)
    overridden = sum(1 for b in blocks if b.override_reason is not None)
    orphans_attributed = sum(
        1 for b in blocks if b.is_orphan and b.attributed_to)

    decisions = {}
    for path, rec in records.items():
        decisions[path] = {
            "state": rec.state,
            "source_block_idxs": rec.source_blocks,
            "chosen_full_idx": rec.chosen_full_idx,
            "chosen_by": rec.dedup_reason,
            "trust": rec.trust,
            "written": path in written,
            "patch_count": len(rec.patches),
            "patch_failures": [
                p["idx"] for p in rec.patches
                if p.get("applied_by") == "failed"
            ],
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
                "orphans_attributed": orphans_attributed,
            },
            "escalations": escalations,
            "cross_validate": cross_validate,
            "missing_files": sorted(missing) if missing else [],
        },
        "files": written,
        "skipped": skipped,
        "decisions": decisions,
        "deleted": all_deleted,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    if skipped:
        by_reason: dict[str, list[str]] = {
            "no_full":    [],
            "low_trust":  [],
            "no_content": [],
            "collision":  [],
            "other":      [],
        }
        for s in skipped:
            r = s.get("reason", "")
            if "no full print" in r:
                by_reason["no_full"].append(s["path"])
            elif "trust=" in r:
                by_reason["low_trust"].append(s["path"])
            elif "content is None" in r:
                by_reason["no_content"].append(s["path"])
            elif "basename collision" in r:
                by_reason["collision"].append(s["path"])
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
        print(f"    Full list: {out_dir / 'warnings.txt'}",
              file=sys.stderr)

    xv = cross_validate.get("problems", [])
    if xv:
        missing_mods = sorted({
            p["module"] for p in xv if p["type"] == "missing_module"
        })
        if missing_mods:
            print(f"  missing modules ({len(missing_mods)}): "
                  f"{', '.join(missing_mods)}", file=sys.stderr)

    print(f"\nWrote {written_count} files to {files_dir}/")
    print(f"  skipped:  {len(skipped)}"
          + ("  (see warnings.txt)" if skipped else ""))

    total_preserved = len(untouched_files) + len(untouched_low)
    if total_preserved:
        print(f"  preserved: {total_preserved} pre-existing file(s) "
              f"left untouched (not part of this run)")
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

    print(f"  classified: {by_heuristic} heuristic / {by_llm} LLM "
          f"(escalated {escalated}/{len(blocks)}, "
          f"{overridden} overridden by hard rules)")
    print(f"  orphans:  {orphans_attributed} attributed to a file"
          + (f", {len(unattributed)} unattributed (see warnings.txt)"
             if unattributed else ""))
    print(f"  graph:    {len(xv)} import problem(s)"
          if xv else "  graph:    clean")
    print(f"  deleted:  {out_dir / 'deleted.txt'} "
          f"({len(all_deleted)} entries)")
    if missing:
        print(f"  missing:  {out_dir / 'MISSING_FILES.txt'} "
              f"({len(missing)} files referenced but never recovered)")
    if llm is not None:
        print(f"  LLM:      {llm.calls_made} live, "
              f"{llm.calls_cached} cached, "
              f"{llm.calls_refused} refused")
    print(f"  warnings: {out_dir / 'warnings.txt'}")
    print(f"  manifest: {out_dir / 'manifest.json'}")
    return exit_code
