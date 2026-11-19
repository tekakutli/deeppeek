#!/usr/bin/env python3
"""dump_corpus.py — parse `dump.py` output and resolve target files.

Sibling of `extract_edit_requests.py`.  Provides:

  · `DumpFile`    — one parsed dump (path, scanned_dir, files, tree).
  · `DumpCorpus`  — collection of DumpFiles with path resolution:
                    exact (abs, then scanned_dir-joined) → suffix →
                    basename, reporting ambiguity across dumps.
  · `_load_dump_corpus`, `_parse_dump_file` — parsing entry points.
  · `_write_retrieved` — write resolved file contents under
                         `<out_dir>/retrieved/<dump-stem>/<relpath>`.

No dependencies on the rest of the pipeline; importable standalone.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional


_HEADER_LINE = "=" * 70
_HEADER_RE = re.compile(r"^" + re.escape(_HEADER_LINE) + r"$")
_FILE_HDR_RE = re.compile(r"^FILE (\d+)/(\d+): (.+)$")
_SCANNED_RE = re.compile(
    r"^SCANNED DIRECTORY:\s+(.+?)(?:\s+\(scan offset:[^)]+\))?\s*$")
_TREE_HDR = "DIRECTORY TREE"


class DumpFile:
    """One parsed `dump.py` output.

    `path`        — the dump file itself
    `scanned_dir` — the absolute path recorded in its SCANNED DIRECTORY
                    header (the tree root at dump time)
    `files`       — ordered map: relpath (as printed after `FILE i/N:`)
                    → file content (str).  Insertion order preserved.
    `tree`        — raw DIRECTORY TREE section text (may be "")
    """

    def __init__(self, path: Path, scanned_dir: str,
                 files: dict[str, str], tree: str = ""):
        self.path = path
        self.scanned_dir = scanned_dir
        self.files = files
        self.tree = tree

    def abs_path_for(self, relpath: str) -> str:
        return os.path.normpath(
            os.path.join(self.scanned_dir, relpath))


class DumpCorpus:
    """Collection of DumpFiles with path resolution.

    `resolve(target)` maps a target-path suggestion (relative, basename-
    only, or absolute) to the best-matching file across all loaded
    dumps.  Multiple dumps can provide the same abs path — every source
    is kept in the result, so callers can tell which dump each retrieved
    file came from.
    """

    def __init__(self) -> None:
        self.dumps: list[DumpFile] = []
        self._by_abs: dict[str, list[tuple[DumpFile, str]]] = {}
        self._by_suffix: dict[tuple[str, ...],
                              list[tuple[DumpFile, str]]] = {}

    def add(self, dump: DumpFile) -> None:
        self.dumps.append(dump)
        for relpath in dump.files:
            entry = (dump, relpath)
            abs_p = dump.abs_path_for(relpath)
            self._by_abs.setdefault(abs_p, []).append(entry)
            parts = tuple(Path(relpath).parts)
            for n in range(1, len(parts) + 1):
                self._by_suffix.setdefault(parts[-n:], []).append(entry)

    def resolve(self, target: str) -> dict:
        if not target:
            return {"status": "not_found", "target": target}

        # 1) Absolute path — direct lookup first.
        if os.path.isabs(target):
            abs_norm = os.path.normpath(target)
            matches = self._by_abs.get(abs_norm, [])
            if matches:
                groups = _group_by_abs(matches)
                if len(groups) == 1:
                    return _resolved_result("exact-abs", target, groups)
                return _ambiguous_result("exact-abs", target, groups)

        target_norm = os.path.normpath(target)
        target_parts = tuple(
            p for p in Path(target_norm).parts if p and p != "/")
        if not target_parts:
            return {"status": "not_found", "target": target}

        # 2) Relative — join with each dump's scanned_dir.
        if not os.path.isabs(target):
            exact: list[tuple[DumpFile, str]] = []
            for dump in self.dumps:
                cand = os.path.normpath(
                    os.path.join(dump.scanned_dir, target_norm))
                exact.extend(self._by_abs.get(cand, []))
            if exact:
                groups = _group_by_abs(exact)
                if len(groups) == 1:
                    return _resolved_result("exact", target, groups)
                return _ambiguous_result("exact", target, groups)

        # 3) Suffix match, longest-first (falls back to basename).
        for n in range(len(target_parts), 0, -1):
            matches = self._by_suffix.get(target_parts[-n:], [])
            if matches:
                groups = _group_by_abs(matches)
                if len(groups) == 1:
                    return _resolved_result("suffix", target, groups)
                return _ambiguous_result("suffix", target, groups)

        return {"status": "not_found", "target": target}


# ---- helpers -----------------------------------------------------

def _group_by_abs(matches):
    groups: dict[str, list[tuple[DumpFile, str]]] = {}
    for dump, relpath in matches:
        groups.setdefault(dump.abs_path_for(relpath), []).append(
            (dump, relpath))
    return groups


def _source_entry(dump: DumpFile, relpath: str) -> dict:
    return {
        "dump_file":   str(dump.path),
        "scanned_dir": dump.scanned_dir,
        "relpath":     relpath,
    }


def _resolved_result(method: str, target: str, groups: dict) -> dict:
    abs_p, sources = next(iter(groups.items()))
    primary_dump, primary_rel = sources[0]
    content = primary_dump.files.get(primary_rel, "")
    return {
        "status":         "resolved",
        "method":         method,
        "target":         target,
        "abs_path":       abs_p,
        "dump_file":      str(primary_dump.path),
        "scanned_dir":    primary_dump.scanned_dir,
        "relpath":        primary_rel,
        "content":        content,
        "content_length": len(content),
        "sources":        [_source_entry(d, rp) for d, rp in sources],
    }


def _ambiguous_result(method: str, target: str, groups: dict) -> dict:
    return {
        "status":     "ambiguous",
        "method":     method,
        "target":     target,
        "candidates": [
            {
                "abs_path": abs_p,
                "sources":  [_source_entry(d, rp) for d, rp in srcs],
            }
            for abs_p, srcs in groups.items()
        ],
    }


# ---- parsing -----------------------------------------------------

def _parse_dump_file(path: Path) -> DumpFile:
    """Parse one `dump.py` output into a `DumpFile`."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    scanned_dir = ""
    for line in lines[:20]:
        m = _SCANNED_RE.match(line)
        if m:
            scanned_dir = m.group(1).strip()
            break

    file_starts: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if idx == 0:
            continue
        fm = _FILE_HDR_RE.match(line)
        if fm and _HEADER_RE.match(lines[idx - 1]):
            file_starts.append((idx, fm.group(3).rstrip()))

    files: dict[str, str] = {}
    for k, (start_idx, relpath) in enumerate(file_starts):
        j = start_idx + 1
        if j < len(lines) and _HEADER_RE.match(lines[j]):
            j += 1
        if j < len(lines) and lines[j].startswith("```"):
            j += 1
        end = (file_starts[k + 1][0] - 1
               if k + 1 < len(file_starts) else len(lines))
        body: list[str] = []
        while j < end:
            if lines[j].strip() == "```":
                break
            body.append(lines[j])
            j += 1
        files[relpath] = "\n".join(body)

    tree_lines: list[str] = []
    for idx, line in enumerate(lines):
        if line.strip() != _TREE_HDR:
            continue
        j = idx + 1
        while j < len(lines) and lines[j].strip() != "```":
            j += 1
        if j >= len(lines):
            break
        j += 1
        while j < len(lines) and lines[j].strip() != "```":
            tree_lines.append(lines[j])
            j += 1
        break

    return DumpFile(path, scanned_dir, files, "\n".join(tree_lines))


def _load_dump_corpus(
        paths: list[str]) -> tuple[DumpCorpus, list[str]]:
    corpus = DumpCorpus()
    errors: list[str] = []
    for raw in paths:
        p = Path(raw).expanduser()
        try:
            p = p.resolve()
        except OSError as e:
            errors.append(f"cannot resolve dump path {raw!r}: {e}")
            continue
        if not p.is_file():
            errors.append(f"dump file not found: {p}")
            continue
        try:
            corpus.add(_parse_dump_file(p))
        except Exception as e:
            errors.append(f"failed to parse {p}: {e}")
    return corpus, errors


# ---- retrieved-file writing -------------------------------------

def _write_retrieved(retrieved_by_target: dict[str, dict],
                     out_dir: Path,
                     quiet: bool = False) -> int:
    """Write uniquely-resolved targets under `<out_dir>/retrieved/`.

    Layout mirrors the dump structure:
        retrieved/<dump-stem>/<relpath-from-dump>
    """
    rdir = out_dir / "retrieved"
    written = 0
    for target, res in retrieved_by_target.items():
        if res.get("status") != "resolved":
            continue
        content = res.get("content")
        if content is None:
            continue
        parts = [p for p in Path(res["relpath"]).parts
                 if p not in ("", ".", "..", "/")]
        if not parts:
            continue
        dst = rdir / Path(res["dump_file"]).stem
        for p in parts:
            dst = dst / p
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
            written += 1
        except Exception as e:
            print(f"  ! failed to write retrieved {target}: {e}",
                  file=sys.stderr)
    return written


if __name__ == "__main__":
    # Tiny manual test:  python dump_corpus.py DUMP.md  target/path.py
    import argparse
    ap = argparse.ArgumentParser(prog="dump_corpus.py")
    ap.add_argument("dumps", nargs="+")
    ap.add_argument("--resolve", action="append", default=[],
                    help="target to resolve (repeatable)")
    args = ap.parse_args()
    corpus, errs = _load_dump_corpus(args.dumps)
    for e in errs:
        print(f"error: {e}", file=sys.stderr)
    print(f"loaded {len(corpus.dumps)} dump(s), "
          f"{sum(len(d.files) for d in corpus.dumps)} file(s)",
          file=sys.stderr)
    import json
    for t in args.resolve:
        print(json.dumps(
            {k: v for k, v in corpus.resolve(t).items() if k != "content"},
            indent=2))
