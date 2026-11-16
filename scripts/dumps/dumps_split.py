#!/usr/bin/env python3
"""
dumps_split.py — split a dump.py-generated dump into N balanced parts.

Every FILE block is fully contained in exactly one output part — no
file is ever truncated.  Parts are written in the SAME dump.py
format, so a part can be fed to any of the sibling tools
(dumps_diff.py, dumps_casted.py, dumps_evolve.py, …) as if it were
a complete dump.  FILE i/N blocks are renumbered per part.

Partition strategy
==================
Files are atomic units.  Balancing uses LPT (Longest Processing
Time first) bin packing:

    1. Estimate each file's line cost:  content lines + 6
       (HEADER, FILE line, HEADER, fence-open, fence-close, blank).
    2. Sort files largest-first.
    3. Assign each to the currently-smallest part.

LPT is a 4/3-approximation of the optimal (minimum-makespan)
partition — close enough for the "roughly equal" goal, and it
handles the common case where one large file forces one part to
be bigger than the rest.  Within each part, files are re-sorted
by their ORIGINAL order so a part reads like a slice of the input.

Per-part DIRECTORY TREE
=======================
Each part gets its own tree, rebuilt from just the paths it
contains (default `--tree rebuild`, overridable via
DEFAULT_TREE_MODE below).  This is more useful than copying the
original tree verbatim: readers see exactly what each part
contains, at its real hierarchy.  Two alternatives:

    --tree keep     copy the original dump's tree verbatim
                    (misleading — it lists files the part
                     does not contain)
    --tree strip    omit the tree entirely

`--tree rebuild` extracts the tree root label from the original
tree (`/path/to/project/` → `/path/to/project`) so the rebuilt
tree is anchored at the same root as the source dump.

A short banner
==============
Every part gets:

    ======================================================================
    PART i/N of original_dump.md
    (K files in this part)
    ======================================================================

followed by the original preamble (SCANNED DIRECTORY header and,
depending on --tree, the per-part DIRECTORY TREE), then the FILE
blocks.  The banner means a reader instantly knows whether
they are looking at part 2 of 5, and a downstream parser just sees
it as preamble — `parse_dump` still finds the FILE blocks.

Output naming
=============
Parts are written as:

    <dump_stem>_part<i>of<N><dump_suffix>

next to the input dump, or into `-o DIR` if given.

Usage
=====
    dumps_split.py project_dump_foo.md                # config default parts
    dumps_split.py project_dump_foo.md -n 3           # 3 parts
    dumps_split.py project_dump_foo.md --target-lines 5000
    dumps_split.py project_dump_foo.md -o /tmp/parts/ # into DIR
    dumps_split.py project_dump_foo.md --tree strip   # no tree
    dumps_split.py project_dump_foo.md --dry-run      # plan only

Companion to dump.py / dumps_diff.py / dumps_evolve.py.
"""

import argparse
import importlib.util
import math
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURATION — edit these
# ---------------------------------------------------------------------------
# Default number of output parts when neither -n/--parts nor
# --target-lines is given on the command line (and DEFAULT_TARGET_LINES
# is disabled/None below).  Reduced to the actual file count (with a
# warning) when the dump has fewer files than this.
DEFAULT_NUM_PARTS = 9

# Alternative to DEFAULT_NUM_PARTS: target a per-part line count.  When
# set to a positive integer, this takes precedence over DEFAULT_NUM_PARTS
# (the effective part count becomes ceil(total_lines / DEFAULT_TARGET_LINES)).
# CLI flags -n/--parts and --target-lines still override both defaults.
# Set to None to disable (i.e. fall back to DEFAULT_NUM_PARTS).
DEFAULT_TARGET_LINES = None
# DEFAULT_TARGET_LINES = 5000

# How each part's DIRECTORY TREE is rendered.  CLI --tree still overrides.
#   "rebuild"  show only the files contained in this part, at their
#              real hierarchy, anchored at the source dump's tree root
#              (extracted from the original preamble).  This is the
#              sensible default: each part's tree truthfully describes
#              that part alone.
#   "keep"     copy the original dump's DIRECTORY TREE verbatim into
#              every part.  Misleading — it lists files the part does
#              not contain — but sometimes useful for diffing.
#   "strip"    omit the DIRECTORY TREE block entirely from every part.
DEFAULT_TREE_MODE = "rebuild"
# DEFAULT_TREE_MODE = "keep"
# DEFAULT_TREE_MODE = "strip"
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DIFF_SCRIPT = SCRIPT_DIR / "dumps_diff.py"

HEADER = "=" * 70
_BT3 = "`" * 3
_FILES = "\0files"

# Six per-file lines: HEADER, "FILE i/N: path", HEADER, fence-open,
# fence-close, blank.  Used for LPT balance and for --target-lines.
_FILE_OVERHEAD_LINES = 6

_TREE_MODES = ("keep", "rebuild", "strip")

_cache = {}


def _load(name, path):
    if name in _cache:
        return _cache[name]
    if not path.is_file():
        raise SystemExit(f"error: required script not found: {path}")
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: could not load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _cache[name] = mod
    return mod


def load_diff():
    return _load("split_diff", DIFF_SCRIPT)


# ---------------------------------------------------------------------------
# Tree rebuilding — powers --tree rebuild.
# ---------------------------------------------------------------------------

def _extract_tree_root(preamble):
    """Return the tree label (e.g. '/path/to/project') from a preamble's
    DIRECTORY TREE, or None if the preamble has no tree."""
    m = re.search(
        re.escape(HEADER) + r"\nDIRECTORY TREE\n" + re.escape(HEADER)
        + r"\n" + _BT3 + r"\n([^\n]+)\n",
        preamble,
    )
    if not m:
        return None
    label = m.group(1).strip()
    if label.endswith("/"):
        label = label[:-1]
    return label or "."


def _build_tree_from_paths(paths):
    """Nested-dict tree of just the given file paths.

    Absolute paths have their root anchor dropped so the tree label
    (which already shows the project root) isn't duplicated as a
    stray '/' directory entry.  Non-path entries are skipped, not
    fatal — robustness over completeness.
    """
    tree = {}
    for path in paths:
        try:
            p = Path(path)
        except (TypeError, ValueError, OSError):
            continue
        parts = p.parts
        if p.is_absolute() and parts:
            parts = parts[1:]
        if not parts:
            continue
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault(_FILES, []).append(parts[-1])
    return tree


def _render_tree_node(node, prefix):
    lines = []
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


def _render_tree(tree, label):
    if label in ("", "."):
        label = "."
    return "\n".join([label + "/"] + _render_tree_node(tree, ""))


def _make_tree_block(paths, tree_root_label):
    text = _render_tree(_build_tree_from_paths(paths), tree_root_label)
    return (
        HEADER + "\n"
        + "DIRECTORY TREE\n"
        + HEADER + "\n"
        + _BT3 + "\n"
        + text + "\n"
        + _BT3 + "\n\n"
    )


def _prepare_preamble(orig_preamble, files, tree_mode):
    """Return the preamble for a part, according to --tree."""
    if tree_mode == "keep":
        return orig_preamble

    stripped = load_diff().strip_tree(orig_preamble)

    if tree_mode == "strip":
        return stripped

    # rebuild (default)
    root_label = _extract_tree_root(orig_preamble) or "."
    return stripped + _make_tree_block([p for p, _ in files], root_label)


# ---------------------------------------------------------------------------
# Partitioning — LPT bin packing.
# ---------------------------------------------------------------------------

def _file_size(content):
    return _FILE_OVERHEAD_LINES + len(content.splitlines())


def _partition_lpt(files, num_parts):
    """Assign files to parts to balance total line count.

    Files are atomic.  LPT: sort largest-first, assign each to the
    currently-smallest part.  Within each part, files are re-sorted
    by their ORIGINAL order so a part reads like a contiguous slice
    of the input dump.

    Returns (parts, part_sizes) where parts[i] is a list of
    (path, content) tuples and part_sizes[i] is its estimated
    line count.
    """
    sizes = [_file_size(c) for _, c in files]
    part_indices = [[] for _ in range(num_parts)]
    part_sizes = [0] * num_parts

    for i in sorted(range(len(files)), key=lambda k: -sizes[k]):
        target = min(range(num_parts), key=lambda p: part_sizes[p])
        part_indices[target].append(i)
        part_sizes[target] += sizes[i]

    parts = [[files[i] for i in sorted(idx_list)]
             for idx_list in part_indices]
    return parts, part_sizes


# ---------------------------------------------------------------------------
# Writing a part.
# ---------------------------------------------------------------------------

def _write_part(out_path, orig_name, part_num, num_parts, preamble, files):
    """Write one dump.py-format part.

    The part banner goes in the preamble, so `dumps_diff.parse_dump`
    still finds the FILE blocks correctly.  FILE i/N is renumbered
    so the part is self-consistent (N = this part's file count).
    """
    total = len(files)
    noun = "file" if total == 1 else "files"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(HEADER + "\n")
        fh.write(f"PART {part_num}/{num_parts} of {orig_name}\n")
        fh.write(f"({total} {noun} in this part)\n")
        fh.write(HEADER + "\n\n")
        fh.write(preamble)

        for i, (path, content) in enumerate(files, 1):
            fh.write(HEADER + "\n")
            fh.write(f"FILE {i}/{total}: {path}\n")
            fh.write(HEADER + "\n")
            fh.write(_BT3 + "python\n")
            fh.write(content)
            if content and not content.endswith("\n"):
                fh.write("\n")
            fh.write(_BT3 + "\n\n")


def _output_path(dump_path, output_dir, part_num, num_parts):
    name = f"{dump_path.stem}_part{part_num}of{num_parts}{dump_path.suffix}"
    if output_dir is None:
        return dump_path.with_name(name)
    return Path(output_dir) / name


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dumps_split.py",
        description=(
            "Split a dump.py-generated dump into N balanced parts.  "
            "Each file is fully contained in exactly one part — files "
            "are never truncated.  Parts are written in dump.py format, "
            "so they can be parsed by dumps_diff.py, dumps_casted.py, "
            "dumps_evolve.py, etc."
        ),
    )
    ap.add_argument("dump", type=Path, metavar="DUMP",
                    help="input dump (as produced by dump.py)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument(
        "-n", "--parts", type=int, default=None, metavar="N",
        help=("number of output parts.  Reduced to the file count if "
              "smaller.  "
              f"(hardcoded default: {DEFAULT_NUM_PARTS})"),
    )
    grp.add_argument(
        "--target-lines", type=int, default=None, metavar="L",
        help=("alternative to --parts: produce parts of roughly L "
              "lines each.  Equivalent to -n ceil(total_lines / L).  "
              f"(hardcoded default: {DEFAULT_TARGET_LINES!r}; "
              "when set, overrides the parts default)"),
    )
    ap.add_argument(
        "-o", "--output-dir", type=Path, default=None, metavar="DIR",
        help="directory to write parts into (default: alongside DUMP)",
    )
    ap.add_argument(
        "--tree", choices=_TREE_MODES, default=DEFAULT_TREE_MODE,
        help=("how to render each part's DIRECTORY TREE:  "
              "keep (copy the original dump's tree verbatim),  "
              "rebuild (show only this part's files),  "
              "strip (omit the tree entirely).  "
              f"(hardcoded default: {DEFAULT_TREE_MODE!r})"),
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="print the proposed partition but don't write any files",
    )
    args = ap.parse_args(argv)

    if not args.dump.is_file():
        print(f"error: not a file: {args.dump}", file=sys.stderr)
        return 1
    if args.parts is not None and args.parts < 1:
        print("error: --parts must be >= 1", file=sys.stderr)
        return 1
    if args.target_lines is not None and args.target_lines < 1:
        print("error: --target-lines must be >= 1", file=sys.stderr)
        return 1

    diff = load_diff()
    try:
        text = args.dump.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read {args.dump}: {e}", file=sys.stderr)
        return 1

    preamble, files = diff.parse_dump(text)
    if not files:
        print(f"error: no FILE blocks parsed from {args.dump}",
              file=sys.stderr)
        return 1

    total_size = sum(_file_size(c) for _, c in files)

    # Resolve the effective number of parts.  Precedence:
    #   1. --target-lines on CLI
    #   2. -n/--parts on CLI
    #   3. DEFAULT_TARGET_LINES (if set to a valid int >= 1)
    #   4. DEFAULT_NUM_PARTS
    # CLI flags are mutually exclusive by argparse; the two config
    # defaults may both be set, in which case DEFAULT_TARGET_LINES
    # wins (mirroring how DEFAULT_SCAN_OFFSET overrides the plain
    # scan in dump.py).  Config values < 1 are treated as "disabled"
    # so a stray 0 or negative value silently falls through instead
    # of crashing at startup.
    if args.target_lines is not None:
        num_parts = max(1, math.ceil(total_size / args.target_lines))
    elif args.parts is not None:
        num_parts = args.parts
    elif DEFAULT_TARGET_LINES is not None and DEFAULT_TARGET_LINES >= 1:
        num_parts = max(1, math.ceil(total_size / DEFAULT_TARGET_LINES))
    else:
        num_parts = DEFAULT_NUM_PARTS

    if num_parts > len(files):
        print(f"warning: requested {num_parts} parts but the dump has "
              f"only {len(files)} file(s); reducing to {len(files)}",
              file=sys.stderr)
        num_parts = len(files)
    if num_parts == 1:
        print("warning: dump fits in a single part", file=sys.stderr)

    parts, part_sizes = _partition_lpt(files, num_parts)

    orig_name = args.dump.name
    print(f"Split {orig_name} ({len(files)} files, ~{total_size} lines) "
          f"into {num_parts} part(s):", file=sys.stderr)

    def _report(part_num, n_files, est, out_path):
        pct = 100.0 * est / max(total_size, 1)
        noun = "file" if n_files == 1 else "files"
        print(f"  {out_path.name}: {n_files} {noun}, ~{est} lines "
              f"({pct:.1f}%)", file=sys.stderr)

    if args.dry_run:
        for i, (part, size) in enumerate(zip(parts, part_sizes), 1):
            out_path = _output_path(args.dump, args.output_dir, i, num_parts)
            _report(i, len(part), size, out_path)
        print("[dry run — no files written]", file=sys.stderr)
        return 0

    if args.output_dir is not None:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"error: cannot create {args.output_dir}: {e}",
                  file=sys.stderr)
            return 1

    for i, part in enumerate(parts, 1):
        part_preamble = _prepare_preamble(preamble, part, args.tree)
        out_path = _output_path(args.dump, args.output_dir, i, num_parts)
        try:
            _write_part(out_path, orig_name, i, num_parts,
                        part_preamble, part)
        except OSError as e:
            print(f"error: cannot write {out_path}: {e}", file=sys.stderr)
            return 1
        _report(i, len(part), part_sizes[i - 1], out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
