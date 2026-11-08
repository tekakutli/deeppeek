#!/usr/bin/env python3
"""
dump_filelist.py — dump the files named in a filelist, in dump.py format.

Reads a filelist — either a plain text file (one path per line) or a
JSON document produced by a structured path extractor — resolves each
entry against BASE_DIR, and writes a markdown dump with the same shape
dump.py produces: SCANNED DIRECTORY header, DIRECTORY TREE, and
FILE i/N blocks.

Filelist provenance
-------------------
The filelist is usually generated, not hand-written.  Two common
sources are supported:

  · Plain text — one path per line:

        deeppeek paths conversation.md > paths.txt

  · JSON — the structured output of a tool that records the
    conversation's paths with source locations and tiers:

        deeppeek paths conversation.md --format json > paths.json

    Any of these JSON shapes is accepted:

      {"structure": [<node>, ...], "root_paths": [...]}
          node      = {"paths": [<entry>, ...],
                       "children": [<node>, ...]}
          entry     = {"path": str, ...}
          This is the shape a doc-hierarchy-aware extractor emits
          (as the JSON in the wild does).

      {"matches": [<entry>, ...]}     — flat form
      {"paths":   [<entry>, ...]}     — flat form
      [<entry>, ...]                  — bare array of objects
      ["path1", "path2", ...]         — bare string array

    Paths are collected recursively through `structure[*].children`,
    then deduplicated (preserving first-occurrence order).  The
    "Resolving" line reports both the raw mention count and the
    unique count, so duplicate-heavy JSON is visible rather than
    silently flattened.

Either way, the resolved paths go through the same resolution chain
(below) and produce an identical-shape dump.

Resolution rules
----------------
For each non-empty, non-comment entry:

  · Entries ending in "/" are directory markers.  By default they
    are skipped (dump.py's explicit-file-list behaviour); with
    --expand-dirs, each is resolved to a directory on disk and every
    file under it (recursively) is included.  The same suffix /
    basename fallbacks that apply to files also apply to directory
    markers when expanding.

  · Absolute paths (starting with "/") that resolve to a file on
    the filesystem are included as-is; otherwise skipped.

  · Relative paths are resolved against BASE_DIR:

      1. Direct hit: BASE_DIR / line is a file → include it.

      2. Exact relative match: `line` matches a file's path relative
         to BASE_DIR exactly.

      3. Suffix match (if the line contains "/"): any file whose
         relative path ends with "/<line>" is included.  This
         handles filelists that use "logical" paths (e.g.
         `api/routes.py`) when the actual file sits deeper (e.g.
         `app/assets/api/routes.py`).

      4. Basename match: any file under BASE_DIR whose basename is
         the same as the line's basename is included.  This
         handles filelists that name a bare filename
         (`event_log.py`) when the file lives at any depth below
         BASE_DIR.

      5. No match: warn to stderr and skip.

  · All resolutions are deduplicated by resolved absolute path.

The dump's tree is built from the RESOLVED files' common parent,
mirroring dump.py's explicit-files mode (so the tree shows the
actual on-disk layout, not the filelist's possibly-inconsistent
layout).

Usage
-----
    dump_filelist.py FILELIST                    # → stdout
    dump_filelist.py FILELIST -o dump.md         # → file
    dump_filelist.py FILELIST --base src/        # resolve against src/

    # Text pipeline:
    deeppeek paths conversation.md > paths.txt
    dump_filelist.py paths.txt --base . -o project_dump_filtered.md

    # JSON pipeline (recursive structure walk, then dedupe):
    deeppeek paths conversation.md --format json > paths.json
    dump_filelist.py paths.json --base . -o project_dump_filtered.md

    # Also expand directory markers (their entire subtree):
    dump_filelist.py paths.json --expand-dirs -o project_dump_full.md

Companion tool to dump.py, but takes an explicit filelist rather
than scanning for file extensions.
"""
import argparse
import json
import os
import sys
from pathlib import Path


HEADER = "=" * 70
_FILES = "\0files"


# ---------------------------------------------------------------------------
# Tree rendering — same shape dump.py uses.
# ---------------------------------------------------------------------------

def _render_tree_node(node: dict, prefix: str) -> list[str]:
    lines: list[str] = []
    dirs = sorted(k for k in node if k != _FILES)
    files = sorted(node.get(_FILES, []))
    entries = [(d, True) for d in dirs] + [(f, False) for f in files]
    for i, (name, is_dir) in enumerate(entries):
        last = i == len(entries) - 1
        connector = "└── " if last else "├── "
        if is_dir:
            lines.append(prefix + connector + name + "/")
            ext = "    " if last else "│   "
            lines.extend(_render_tree_node(node[name], prefix + ext))
        else:
            lines.append(prefix + connector + name)
    return lines


def _build_tree(tree_root: Path, files: list[Path]) -> dict:
    tree: dict = {}
    for f in files:
        try:
            rel = f.relative_to(tree_root)
        except ValueError:
            continue
        node = tree
        for part in rel.parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault(_FILES, []).append(rel.parts[-1])
    return tree


def _render_tree(tree: dict, label: str) -> str:
    if label in ("", "."):
        label = "."
    lines = [label + "/"]
    lines.extend(_render_tree_node(tree, ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Input loading — text filelist or JSON.
# ---------------------------------------------------------------------------

def _read_text_filelist(raw: str) -> list[str]:
    out: list[str] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _extract_paths_from_json(data) -> list[str]:
    """Collect every path string from any of the accepted JSON shapes.

    Traverses `structure` recursively (children included), and also
    picks up the flat `matches` / `paths` / `root_paths` arrays if
    present.  Order of first mention is preserved; duplicates are
    NOT removed here (the caller does that so the raw count can be
    reported).
    """
    out: list[str] = []

    def _consume(item) -> None:
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
        elif isinstance(item, dict):
            p = item.get("path")
            if isinstance(p, str) and p.strip():
                out.append(p.strip())

    def _walk(node) -> None:
        if not isinstance(node, dict):
            return
        for p in node.get("paths") or []:
            _consume(p)
        for child in node.get("children") or []:
            _walk(child)

    if isinstance(data, list):
        for item in data:
            _consume(item)
        return out

    if not isinstance(data, dict):
        return out

    # Flat lists at the top level.
    for key in ("root_paths", "matches", "paths"):
        val = data.get(key)
        if isinstance(val, list):
            for p in val:
                _consume(p)

    # Nested structure.
    for node in data.get("structure") or []:
        _walk(node)

    return out


def _load_input(path: Path) -> tuple[list[str], str, int]:
    """Return (deduped_entries, format_label, raw_count).

    `format_label` is "text" or "json".  For text, `raw_count` equals
    `len(entries)`.  For JSON it is the pre-dedupe mention count.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"error: cannot read {path}: {e}")

    is_json_ext = path.suffix.lower() == ".json"
    might_be_json = is_json_ext or raw.lstrip().startswith(("{", "["))

    if might_be_json:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            if is_json_ext:
                raise SystemExit(
                    f"error: {path} has a .json extension but "
                    f"failed to parse: {e}"
                )
            # Content sniff was wrong — fall through to text mode.
            data = None

        if data is not None:
            raw_paths = _extract_paths_from_json(data)
            if raw_paths:
                seen: set[str] = set()
                unique: list[str] = []
                for p in raw_paths:
                    if p in seen:
                        continue
                    seen.add(p)
                    unique.append(p)
                return unique, "json", len(raw_paths)

            if is_json_ext:
                raise SystemExit(
                    f"error: {path} parses as JSON but no path "
                    f"entries were found (looked for 'structure', "
                    f"'matches', 'paths', 'root_paths', or a bare "
                    f"array)"
                )
            # Content-sniff succeeded but nothing to extract —
            # fall through to text mode, in case the first line
            # happened to look like JSON.

    entries = _read_text_filelist(raw)
    return entries, "text", len(entries)


# ---------------------------------------------------------------------------
# File and directory indexing.
# ---------------------------------------------------------------------------

def _index_files(base: Path):
    all_files: list[Path] = []
    by_relpath: dict[str, list[Path]] = {}
    by_basename: dict[str, list[Path]] = {}
    by_dirpath: dict[str, list[Path]] = {}
    for p in base.rglob("*"):
        try:
            rel = p.relative_to(base)
            rp = p.resolve()
        except OSError:
            continue
        rel_str = str(rel).replace(os.sep, "/")
        if p.is_file():
            all_files.append(rp)
            by_relpath.setdefault(rel_str, []).append(rp)
            by_basename.setdefault(p.name, []).append(rp)
        elif p.is_dir():
            by_dirpath.setdefault(rel_str, []).append(rp)
    return all_files, by_relpath, by_basename, by_dirpath


# ---------------------------------------------------------------------------
# Resolution.
# ---------------------------------------------------------------------------

def _resolve_file_line(line: str, base: Path,
                       by_relpath: dict[str, list[Path]],
                       by_basename: dict[str, list[Path]]
                       ) -> tuple[list[Path], str]:
    """Resolve a non-directory entry to a list of files."""
    if line.startswith("/"):
        p = Path(line)
        if p.is_file():
            return [p.resolve()], "absolute"
        return [], "not-found"

    direct = base / line
    if direct.is_file():
        return [direct.resolve()], "direct"

    norm = line.replace(os.sep, "/")
    if norm in by_relpath:
        return list(by_relpath[norm]), "direct"

    if "/" in norm:
        tail = "/" + norm
        suffix_hits: list[Path] = []
        for rel_str, paths in by_relpath.items():
            if rel_str.endswith(tail):
                suffix_hits.extend(paths)
        if suffix_hits:
            return suffix_hits, "suffix"

    basename = Path(norm).name
    if basename in by_basename:
        return list(by_basename[basename]), "basename"

    return [], "not-found"


def _files_under(dir_path: Path, all_files: list[Path]) -> list[Path]:
    """All indexed files whose path is inside `dir_path`."""
    try:
        target = dir_path.resolve()
    except OSError:
        return []
    out: list[Path] = []
    for f in all_files:
        try:
            if f.is_relative_to(target):
                out.append(f)
        except (ValueError, OSError):
            continue
    return out


def _resolve_dir_line(line: str, base: Path,
                      by_dirpath: dict[str, list[Path]],
                      all_files: list[Path]
                      ) -> tuple[list[Path], str]:
    """Resolve a directory marker (line ends in "/") to its files.

    Uses the same suffix and basename fallbacks that apply to files,
    because JSON extractors routinely emit bare directory names
    (`routes/`, `services/`) that only make sense at some depth of
    the real tree.
    """
    norm = line.rstrip("/").replace(os.sep, "/")
    if not norm:
        return [], "directory"

    # Direct hit.
    direct = base / norm
    if direct.is_dir():
        return _files_under(direct, all_files), "expanded-dir"

    # Exact relative dir.
    if norm in by_dirpath:
        out: list[Path] = []
        for d in by_dirpath[norm]:
            out.extend(_files_under(d, all_files))
        return out, "expanded-dir"

    # Suffix match.
    if "/" in norm:
        tail = "/" + norm
        hits: list[Path] = []
        for rel_str, dirs in by_dirpath.items():
            if rel_str.endswith(tail):
                for d in dirs:
                    hits.extend(_files_under(d, all_files))
        if hits:
            return hits, "expanded-dir"

    # Basename match.
    basename = norm.rsplit("/", 1)[-1] if "/" in norm else norm
    hits: list[Path] = []
    for dirs in by_dirpath.values():
        for d in dirs:
            if d.name == basename:
                hits.extend(_files_under(d, all_files))
    if hits:
        return hits, "expanded-dir"
    return [], "directory"


# ---------------------------------------------------------------------------
# Dump writer — same shape dump.py uses for FILE blocks.
# ---------------------------------------------------------------------------

def _format_files(fh, files: list[Path], tree_root: Path) -> None:
    total = len(files)
    for i, f in enumerate(files, 1):
        try:
            display = f.relative_to(tree_root)
        except ValueError:
            display = f
        fh.write(HEADER + "\n")
        fh.write(f"FILE {i}/{total}: {display}\n")
        fh.write(HEADER + "\n")
        fh.write("```python\n")
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            content = f"# ERROR READING FILE: {e}\n"
        fh.write(content)
        if content and not content.endswith("\n"):
            fh.write("\n")
        fh.write("```\n\n")


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dump_filelist.py",
        description="Dump files named in a filelist into a single "
                    "markdown file with a directory tree (same shape "
                    "as dump.py).  Accepts a plain text filelist "
                    "(one path per line) or the JSON output of a "
                    "structured path extractor such as "
                    "`deeppeek paths --format json`.",
    )
    ap.add_argument(
        "filelist", type=Path, metavar="FILELIST",
        help="either a plain text filelist (one path per line; "
             "empty lines and `#` comments are skipped) or a JSON "
             "document (see module docstring for accepted shapes)",
    )
    ap.add_argument(
        "--base", type=Path, default=Path("."), metavar="DIR",
        help="base directory for resolving relative paths "
             "(default: current directory)",
    )
    ap.add_argument(
        "--expand-dirs", action="store_true",
        help="expand directory entries (lines ending in \"/\") to "
             "every file under the resolved directory, recursively.  "
             "Without this flag, directory entries are skipped "
             "(dump.py's explicit-file-list behaviour).  Suffix and "
             "basename fallbacks apply to directory markers too, "
             "so a bare `routes/` from a JSON extractor can resolve "
             "to `app/assets/api/routes/` when that is the only "
             "match.",
    )
    ap.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="output path.  \"-\" writes to stdout (default: stdout).",
    )
    args = ap.parse_args(argv)

    if not args.filelist.is_file():
        print(f"error: not a file: {args.filelist}", file=sys.stderr)
        return 1

    base = args.base.resolve()
    if not base.is_dir():
        print(f"error: --base is not a directory: {base}",
              file=sys.stderr)
        return 1

    entries, fmt_label, raw_count = _load_input(args.filelist)
    if not entries:
        print(f"error: no entries found in {args.filelist}",
              file=sys.stderr)
        return 1

    print(f"Indexing {base} ...", file=sys.stderr)
    all_files, by_relpath, by_basename, by_dirpath = _index_files(base)
    print(f"  {len(all_files)} file(s) indexed", file=sys.stderr)

    plural = "entry" if len(entries) == 1 else "entries"
    msg = f"Resolving {len(entries)} {plural} from {args.filelist}"
    if fmt_label == "json" and raw_count != len(entries):
        msg += f" (deduplicated from {raw_count} raw JSON mentions)"
    print(msg + " ...", file=sys.stderr)

    resolved: list[Path] = []
    seen_files: set[Path] = set()
    counts: dict[str, int] = {}
    not_found: list[str] = []

    for line in entries:
        is_dir_marker = line.endswith("/")

        if is_dir_marker:
            if args.expand_dirs:
                paths, kind = _resolve_dir_line(
                    line, base, by_dirpath, all_files)
            else:
                paths, kind = [], "directory"
        else:
            paths, kind = _resolve_file_line(
                line, base, by_relpath, by_basename)

        counts[kind] = counts.get(kind, 0) + 1
        if kind == "not-found":
            not_found.append(line)
            continue
        if len(paths) > 1:
            print(f"  note: {line!r} matched {len(paths)} file(s); "
                  f"including all", file=sys.stderr)
        for p in paths:
            if p not in seen_files:
                seen_files.add(p)
                resolved.append(p)

    if not resolved:
        print("error: no files resolved from the filelist",
              file=sys.stderr)
        return 1

    print(file=sys.stderr)
    for label in ("direct", "suffix", "basename", "absolute",
                  "expanded-dir", "directory", "not-found"):
        n = counts.get(label, 0)
        if n:
            print(f"  {label:>13}: {n}", file=sys.stderr)

    if not_found:
        nf_plural = "entry" if len(not_found) == 1 else "entries"
        print(f"\n  {len(not_found)} {nf_plural} did not resolve:",
              file=sys.stderr)
        for line in not_found[:20]:
            print(f"    {line}", file=sys.stderr)
        if len(not_found) > 20:
            print(f"    ... and {len(not_found) - 20} more",
                  file=sys.stderr)

    resolved.sort()

    try:
        common = Path(os.path.commonpath([str(p) for p in resolved]))
    except ValueError:
        common = base
    if common.is_file():
        common = common.parent
    if not common.is_dir():
        common = common.parent

    tree = _build_tree(common, resolved)
    tree_text = _render_tree(tree, str(common))

    to_stdout = args.output is None or str(args.output) == "-"
    if to_stdout:
        fh = sys.stdout
        close_fh = False
    else:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            fh = args.output.open("w", encoding="utf-8")
            close_fh = True
        except OSError as e:
            print(f"error: cannot write {args.output}: {e}",
                  file=sys.stderr)
            return 1

    try:
        fh.write(HEADER + "\n")
        fh.write(f"SCANNED DIRECTORY: {common}\n")
        fh.write(HEADER + "\n\n")

        fh.write(HEADER + "\n")
        fh.write("DIRECTORY TREE\n")
        fh.write(HEADER + "\n")
        fh.write("```\n")
        fh.write(tree_text + "\n")
        fh.write("```\n\n")

        _format_files(fh, resolved, common)
    finally:
        if close_fh:
            fh.close()

    target = "<stdout>" if to_stdout else str(args.output)
    print(f"\nWrote {len(resolved)} file(s) to {target}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
