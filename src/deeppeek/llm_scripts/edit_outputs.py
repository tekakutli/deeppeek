#!/usr/bin/env python3
"""edit_outputs.py — file naming, edit-map building, blob writing.

Sibling of `extract_edit_requests.py`.  Provides:

  · `_EXT`, `_ext_for`
  · `_clean_target_path`
  · `_flat_name_for`, `_non_edit_flat_name_for`, `_dedup_flat_names`
  · `_build_edit_map` (returns `(edit_map, target_index)`)
  · `_write_blobs` (copies raw blobs → `files/` with flat names)
  · `_write_edited_files` (writes LLM-produced full files under
    `edited/<target-path>`)

No dependencies on the rest of the pipeline; importable standalone.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional


_EXT = {
    "python": "py", "py": "py", "py3": "py", "python3": "py",
    "javascript": "js", "js": "js", "jsx": "jsx",
    "typescript": "ts", "ts": "ts", "tsx": "tsx",
    "json": "json", "yaml": "yaml", "yml": "yml",
    "css": "css", "scss": "scss",
    "html": "html", "htm": "html",
    "bash": "sh", "sh": "sh", "shell": "sh",
    "sql": "sql", "rust": "rs", "go": "go",
    "c": "c", "cpp": "cpp", "c++": "cpp",
    "java": "java", "kotlin": "kt", "ruby": "rb",
    "markdown": "md", "md": "md", "text": "txt",
    "": "txt",
}


def _ext_for(lang: str) -> str:
    lang = (lang or "").lower()
    if lang in _EXT:
        return _EXT[lang]
    s = "".join(c for c in lang if c.isalnum())
    return s[:4] if s else "txt"


def _clean_target_path(suggestion: Optional[str]) -> Optional[str]:
    if not suggestion:
        return None
    p = Path(suggestion.replace("\\", "/"))
    if p.is_absolute():
        p = Path(*p.parts[1:])
    parts = [x for x in p.parts if x and x not in (".", "..")]
    if not parts:
        return None
    if not parts[-1]:
        return None
    return str(Path(*parts))


def _flat_name_for(rec: dict) -> str:
    target = rec.get("target_file")
    idx = rec["index"]
    if target:
        p = Path(target)
        stem = p.stem or "unknown"
        ext = p.suffix.lstrip(".") or _ext_for(rec.get("lang"))
    else:
        stem = "unknown"
        ext = _ext_for(rec.get("lang"))
    return f"{stem}__blk{idx:03d}.{ext}"


def _non_edit_flat_name_for(rec: dict) -> str:
    return (f"nonedit__blk{rec['index']:03d}."
            f"{_ext_for(rec.get('lang'))}")


def _dedup_flat_names(records: list[dict]) -> None:
    used: set[str] = set()
    for rec in records:
        name = rec.get("flat_filename")
        if not name:
            continue
        if name not in used:
            used.add(name)
            continue
        p = Path(name)
        n = 2
        while True:
            cand = f"{p.stem}_{n}{p.suffix}"
            if cand not in used:
                rec["flat_filename"] = cand
                used.add(cand)
                break
            n += 1


def _build_edit_map(
    records: list[dict], files_dir: Path, *,
    include_non_edits: bool,
) -> tuple[dict, dict]:
    files_dir.mkdir(parents=True, exist_ok=True)

    for rec in records:
        if rec.get("is_edit_request"):
            rec["flat_filename"] = _flat_name_for(rec)
            rec["target_path"] = rec.get("target_file")
        elif include_non_edits:
            rec["flat_filename"] = _non_edit_flat_name_for(rec)
            rec["target_path"] = None
        else:
            rec["flat_filename"] = None
            rec["target_path"] = None

    _dedup_flat_names(records)

    edit_map = {
        "flat_dir": str(files_dir),
        "description": (
            "Edit-request map.  Each entry corresponds to one code "
            "blob in files/ (edit requests only by default; with "
            "`--include-non-edits` also non-edit blobs, whose "
            "`is_edit_request` is false and `target_path` is null). "
            "`target_path` is the file the block appears to modify. "
            "When `target_path` is null the block is a likely edit "
            "but the target could not be resolved."
        ),
        "entries": [
            {
                "flat_filename":   rec["flat_filename"],
                "target_path":     rec.get("target_path"),
                "block_index":     rec["index"],
                "block_filename":  rec.get("block_filename"),
                "is_edit_request": rec["is_edit_request"],
                "edit_kind":       rec.get("edit_kind"),
                "confidence":      rec.get("confidence"),
                "classification":  rec.get("classification"),
            }
            for rec in records if rec.get("flat_filename")
        ],
    }

    targets: dict[str, dict] = {}
    unresolved: list[dict] = []
    non_edits: list[dict] = []
    confirmed_full_files: list[dict] = []
    for rec in records:
        if not rec.get("is_edit_request"):
            non_edits.append({
                "block_index":    rec["index"],
                "block_filename": rec.get("block_filename"),
                "flat_filename":  rec.get("flat_filename"),
                "confidence":     rec.get("confidence"),
                "classification": rec.get("classification"),
                "reasoning":      rec.get("reasoning"),
            })
            if rec.get("classification") == "full_file_skip":
                confirmed_full_files.append({
                    "block_index":    rec["index"],
                    "block_filename": rec.get("block_filename"),
                    "lang":           rec.get("lang"),
                    "reasoning":      rec.get("reasoning"),
                    "code_blob_analysis":
                        rec.get("code_blob_analysis"),
                })
            continue

        entry = {
            "block_index":    rec["index"],
            "block_filename": rec.get("block_filename"),
            "flat_filename":  rec.get("flat_filename"),
            "edit_kind":      rec.get("edit_kind"),
            "confidence":     rec.get("confidence"),
            "classification": rec.get("classification"),
            "reasoning":      rec.get("reasoning"),
        }
        target = rec.get("target_path")
        if target:
            targets.setdefault(target, {"edits": [], "block_count": 0})
            targets[target]["edits"].append(entry)
            targets[target]["block_count"] += 1
        else:
            unresolved.append(entry)

    target_index = {
        "description": (
            "Target-file reverse index.  `targets` maps each file "
            "the conversation is asking to edit to the list of "
            "blocks that edit it.  `unresolved_edits` are likely "
            "edits whose target file could not be determined.  "
            "`non_edits` are blocks the classifier rejected.  "
            "`confirmed_full_files` is the subset of non-edits that "
            "were pre-filtered as full files by code_blob_analysis."
        ),
        "targets":              targets,
        "unresolved_edits":     unresolved,
        "non_edits":            non_edits,
        "confirmed_full_files": confirmed_full_files,
    }
    return edit_map, target_index


def _write_blobs(raw_dir: Path, out_dir: Path,
                 records: list[dict], *,
                 quiet: bool) -> tuple[int, int]:
    written = skipped = 0
    for rec in records:
        flat = rec.get("flat_filename")
        if not flat:
            skipped += 1
            if not quiet and rec.get("is_edit_request") is not True:
                print(f"  · skip  {rec['block_filename']}  "
                      f"({rec.get('classification') or 'non-edit'})",
                      file=sys.stderr)
            continue
        src = raw_dir / rec["block_filename"]
        if not src.is_file():
            print(f"  ! missing blob: {src}", file=sys.stderr)
            skipped += 1
            continue
        dst = out_dir / flat
        shutil.copyfile(src, dst)
        written += 1
        if not quiet:
            kind = "edit" if rec["is_edit_request"] else "nonedit"
            target = rec.get("target_file") or "(unresolved)"
            print(f"  → write [{kind}] {rec['block_filename']}  → "
                  f"files/{flat}   (target: {target})",
                  file=sys.stderr)
    return written, skipped


def _write_edited_files(
    edits_by_target: dict[str, str],
    out_dir: Path,
) -> tuple[int, list[dict]]:
    """Write each LLM-produced full file under `edited/<target>`.

    `edits_by_target` maps target-path → edited full content.  Returns
    `(written_count, log_entries)`.
    """
    edir = out_dir / "edited"
    log: list[dict] = []
    written = 0
    for target, content in edits_by_target.items():
        parts = [p for p in Path(target).parts
                 if p not in ("", ".", "..", "/")]
        if not parts:
            log.append({"target": target,
                        "error": "empty target path"})
            continue
        dst = edir
        for p in parts:
            dst = dst / p
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
            written += 1
            log.append({
                "target":        target,
                "output_path":   str(dst),
                "output_length": len(content),
            })
        except Exception as e:
            log.append({"target": target,
                        "error": f"write failed: {e}"})
    return written, log
