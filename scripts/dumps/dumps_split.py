#!/usr/bin/env python3
"""
dumps_split.py — split a dump.py-generated dump into N balanced parts.

Every FILE block is fully contained in exactly one output part — no
file is ever truncated.  Parts are written in the SAME dump.py
format, so a part can be fed to any of the sibling tools
(dumps_diff.py, dumps_casted.py, dumps_evolve.py, …) as if it were
a complete dump.  FILE i/N blocks are renumbered per part.

TWO PARTITIONING MODES
======================

1.  FIT MODE (default) — `--max-part-bytes N`
    The part count is chosen AUTOMATICALLY so that every part
    (measured in bytes, after banner + preamble + headers + content)
    is strictly smaller than `N` bytes.

    Algorithm:
      a. Estimate part count = ceil(total_content_bytes / max_bytes).
      b. Partition into that many parts (byte-balanced LPT).
      c. Measure the actual byte size of every part.
      d. If the largest part is >= max_bytes, increment the count
         by 1 and retry from (b).
      e. Stop when all parts fit, or when the count reaches
         len(files) (each file in its own part — the maximum split
         that this tool can achieve).

    This is exactly the "roughly estimate → split → rank largest →
    +1 → repeat" behaviour requested for DeepSeek uploads, where
    individual parts must stay under a fixed per-file limit.

    Override `--max-part-bytes` per-run, or edit
    `DEFAULT_MAX_PART_BYTES` below (single source of truth).

2.  FIXED COUNT / TARGET LINES — `-n N` / `--target-lines L`
    Explicit part count OR explicit target-lines-per-part.  Same
    behaviour as before this commit.  Takes precedence over fit
    mode when given.

Precedence (highest first):
    -n/--parts
    --target-lines
    --max-part-bytes (default: DEFAULT_MAX_PART_BYTES)
    --no-fit            → disables fit; falls back to DEFAULT_NUM_PARTS

Per-part DIRECTORY TREE
=======================
Each part gets its own tree, rebuilt from just the paths it
contains (default `--tree rebuild`).  Two alternatives:

    --tree keep     copy the original dump's tree verbatim
    --tree strip    omit the tree entirely

`--tree rebuild` extracts the tree root label from the original
tree so the rebuilt tree is anchored at the same root.

Output naming
=============
Parts are written as:

    <dump_stem>_part<i>of<N><dump_suffix>

next to the input dump, or into `-o DIR` if given.

Usage
=====
    dumps_split.py project_dump_foo.md                # fit to 125 KiB (default)
    dumps_split.py project_dump_foo.md -n 9           # fixed count (old default)
    dumps_split.py project_dump_foo.md --target-lines 5000
    dumps_split.py project_dump_foo.md --max-part-bytes 200000
    dumps_split.py project_dump_foo.md --no-fit       # -n DEFAULT_NUM_PARTS
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
# Default number of output parts when using fixed-count mode (`-n`).
# Reduced to the actual file count (with a warning) when the dump has
# fewer files than this.
DEFAULT_NUM_PARTS = 9

# Alternative to DEFAULT_NUM_PARTS: target a per-part line count.
# When set to a positive integer, takes precedence over DEFAULT_NUM_PARTS
# (in --target-lines mode).  CLI flags -n/--parts and --target-lines
# still override both defaults.
DEFAULT_TARGET_LINES = None

# FIT MODE threshold — the maximum byte size of a single output part.
# Every part must be strictly smaller than this after the full
# banner + preamble + per-file header + content + closer is written
# to disk.
#
# 125 KiB = 128_000 bytes.  This is the value used by the DeepSeek
# upload path — parts larger than this are rejected by the composer.
# CHANGE THIS CONSTANT (or pass --max-part-bytes at CLI) to adjust the
# limit; nothing else in the codebase hardcodes a size.
#
# Set to 0 or None to disable fit mode (fit becomes an explicit
# `--max-part-bytes` opt-in instead of the default).
DEFAULT_MAX_PART_BYTES = 125 * 1024

# How each part's DIRECTORY TREE is rendered.  CLI --tree still overrides.
#   "rebuild"  show only the files contained in this part (default)
#   "keep"     copy the original dump's DIRECTORY TREE verbatim
#   "strip"    omit the DIRECTORY TREE block entirely
DEFAULT_TREE_MODE = "rebuild"
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DIFF_SCRIPT = SCRIPT_DIR / "dumps_diff.py"

HEADER = "=" * 70
_BT3 = "`" * 3
_FILES = "\0files"

# Six per-file lines: HEADER, "FILE i/N: path", HEADER, fence-open,
# fence-close, blank.  Used for line-based LPT balance and
# --target-lines.
_FILE_OVERHEAD_LINES = 6

# Rough byte overhead per file block for the LPT size estimate used
# in fit mode.  Not exact — the actual writer measures precisely in
# `_measure_part_bytes`.  This is only for the initial partitioning
# step's cost estimate.
_FILE_OVERHEAD_BYTES = 220

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
    """Nested-dict tree of just the given file paths."""
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

    root_label = _extract_tree_root(orig_preamble) or "."
    return stripped + _make_tree_block([p for p, _ in files], root_label)


# ---------------------------------------------------------------------------
# Partitioning — LPT bin packing (line-based and byte-based).
# ---------------------------------------------------------------------------

def _file_size(content):
    """Line-based size for LPT and --target-lines."""
    return _FILE_OVERHEAD_LINES + len(content.splitlines())


def _file_size_bytes(content):
    """Byte-based rough size for fit-mode LPT balancing."""
    return _FILE_OVERHEAD_BYTES + len(content.encode("utf-8"))


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


def _partition_lpt_bytes(files, num_parts):
    """Byte-based LPT partition.  Same algorithm as `_partition_lpt`
    but using byte size (content + per-file overhead) for balancing.

    Returns (parts, part_sizes) where part_sizes are rough byte
    totals WITHOUT preamble — that's added later by `_measure_part_bytes`
    when we verify the exact on-disk size.
    """
    sizes = [_file_size_bytes(c) for _, c in files]
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
# Fit mode: measure exact part bytes, iterate parts count until all fit.
# ---------------------------------------------------------------------------

def _measure_part_bytes(part_preamble, part, part_num, num_parts,
                        orig_name):
    """Exact byte count of what `_write_part` will produce for this
    part, including banner, preamble, per-file headers, content,
    closer, and newlines.  This is the ground truth the fit loop uses.
    """
    noun = "file" if len(part) == 1 else "files"
    total = (
        len(HEADER.encode("utf-8")) + 1                # HEADER\n
        + len(f"PART {part_num}/{num_parts} of "
              f"{orig_name}".encode("utf-8")) + 1
        + len(f"({len(part)} {noun} in this part)".encode("utf-8")) + 1
        + len(HEADER.encode("utf-8")) + 2              # HEADER\n\n
    )
    total += len(part_preamble.encode("utf-8"))

    for i, (path, content) in enumerate(part, 1):
        cb = len(content.encode("utf-8"))
        tail = 1 if (content and not content.endswith("\n")) else 0
        total += (
            len(HEADER.encode("utf-8")) + 1
            + len(f"FILE {i}/{len(part)}: {path}".encode("utf-8")) + 1
            + len(HEADER.encode("utf-8")) + 1
            + len(_BT3.encode("utf-8")) + len(b"python") + 1
            + cb + tail
            + len(_BT3.encode("utf-8")) + 2            # ```\n\n
        )
    return total


def _fit_parts(orig_preamble, files, tree_mode, orig_name, max_bytes,
               max_iterations=100):
    """Iteratively increase part count until every part fits under
    `max_bytes`.

    Returns (parts, num_parts, part_bytes_list) where part_bytes_list
    is the exact byte count for each part at the chosen split.

    Raises SystemExit with a warning if even `len(files)` parts
    cannot satisfy the limit (i.e. some single file is bigger than
    `max_bytes` on its own — this tool cannot split a single file).
    """
    if not files:
        return [], 0, []

    if max_bytes <= 0:
        raise SystemExit(
            f"error: --max-part-bytes must be > 0, got {max_bytes}")

    # Rough initial estimate.  Uses total content bytes / max_bytes
    # with 20% headroom for headers + preamble growth as parts shrink.
    total_content = sum(len(c.encode("utf-8")) for _, c in files)
    est = max(1, math.ceil(total_content / (max_bytes * 0.8)))
    num_parts = min(est, len(files))

    last_max = 0
    last_parts = None
    last_part_bytes = None

    for it in range(max_iterations):
        parts, _rough_sizes = _partition_lpt_bytes(files, num_parts)
        part_bytes: list[int] = []
        for i, part in enumerate(parts, 1):
            part_preamble = _prepare_preamble(orig_preamble, part, tree_mode)
            part_bytes.append(
                _measure_part_bytes(
                    part_preamble, part, i, num_parts, orig_name,
                )
            )
        last_parts = parts
        last_part_bytes = part_bytes
        last_max = max(part_bytes) if part_bytes else 0

        if last_max < max_bytes:
            return parts, num_parts, part_bytes

        if num_parts >= len(files):
            print(
                f"warning: --max-part-bytes {max_bytes:,} cannot be "
                f"satisfied with {num_parts} part(s): largest part is "
                f"{last_max:,} bytes.  A single file likely exceeds "
                f"the limit on its own — this tool splits files, not "
                f"content within a file.",
                file=sys.stderr,
            )
            return last_parts, num_parts, last_part_bytes

        num_parts += 1

    print(
        f"warning: fit gave up after {max_iterations} iteration(s); "
        f"using {num_parts} part(s) with largest {last_max:,} bytes",
        file=sys.stderr,
    )
    return last_parts, num_parts, last_part_bytes


# ---------------------------------------------------------------------------
# Writing a part.
# ---------------------------------------------------------------------------

def _write_part(out_path, orig_name, part_num, num_parts, preamble, files):
    """Write one dump.py-format part."""
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
            "Split a dump.py-generated dump into parts.  Every file is "
            "fully contained in exactly one part — files are never "
            "truncated.  Parts are written in dump.py format, so they "
            "can be parsed by dumps_diff.py, dumps_casted.py, "
            "dumps_evolve.py, etc.\n\n"
            "Modes (precedence high-to-low):\n"
            "  -n/--parts N            explicit part count\n"
            "  --target-lines L        target L lines per part\n"
            "  --max-part-bytes N      fit mode: iterate parts count\n"
            "                          until every part fits under N\n"
            "                          bytes (DEFAULT: "
            f"{DEFAULT_MAX_PART_BYTES:,} bytes)\n"
            "  --no-fit                disable fit; use -n with "
            f"DEFAULT_NUM_PARTS ({DEFAULT_NUM_PARTS})"
        ),
    )
    ap.add_argument("dump", type=Path, metavar="DUMP",
                    help="input dump (as produced by dump.py)")

    group = ap.add_mutually_exclusive_group()
    group.add_argument(
        "-n", "--parts", type=int, default=None, metavar="N",
        help=("explicit number of output parts.  Reduced to the file "
              "count if smaller.  Takes precedence over "
              "--max-part-bytes."),
    )
    group.add_argument(
        "--target-lines", type=int, default=None, metavar="L",
        help=("target L lines per part.  Equivalent to "
              "-n ceil(total_lines / L).  Takes precedence over "
              "--max-part-bytes."),
    )
    group.add_argument(
        "--max-part-bytes", type=int, default=None, metavar="N",
        help=("fit mode: iteratively increase part count until every "
              "part is strictly smaller than N bytes (after banner + "
              "preamble + headers + content).  Set to 0 to disable "
              "fit and fall back to DEFAULT_NUM_PARTS.  "
              f"(default: {DEFAULT_MAX_PART_BYTES:,})"),
    )

    ap.add_argument(
        "--no-fit", action="store_true",
        help=("disable fit mode entirely; use DEFAULT_NUM_PARTS "
              f"({DEFAULT_NUM_PARTS}) unless -n or --target-lines "
              "is given.  Shorthand for --max-part-bytes 0."),
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
              f"(default: {DEFAULT_TREE_MODE!r})"),
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
    if args.max_part_bytes is not None and args.max_part_bytes < 0:
        print("error: --max-part-bytes must be >= 0", file=sys.stderr)
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

    orig_name = args.dump.name
    total_size_lines = sum(_file_size(c) for _, c in files)
    total_size_bytes = sum(len(c.encode("utf-8")) for _, c in files)

    # ---- resolve mode ----
    mode = None
    num_parts: int = 0
    parts: list[tuple[str, str]] = []
    part_sizes: list[int] = []
    is_byte_mode = False
    fit_max_bytes = 0

    if args.target_lines is not None:
        mode = "target_lines"
        num_parts = max(1, math.ceil(total_size_lines / args.target_lines))
        if num_parts > len(files):
            print(f"warning: requested {num_parts} parts but the dump "
                  f"has only {len(files)} file(s); reducing to "
                  f"{len(files)}",
                  file=sys.stderr)
            num_parts = len(files)
        parts, part_sizes = _partition_lpt(files, num_parts)

    elif args.parts is not None:
        mode = "parts"
        num_parts = args.parts
        if num_parts > len(files):
            print(f"warning: requested {num_parts} parts but the dump "
                  f"has only {len(files)} file(s); reducing to "
                  f"{len(files)}",
                  file=sys.stderr)
            num_parts = len(files)
        parts, part_sizes = _partition_lpt(files, num_parts)

    else:
        # Fit mode is the default; --no-fit forces fallback.
        eff_max = args.max_part_bytes
        if eff_max is None:
            eff_max = DEFAULT_MAX_PART_BYTES
        if args.no_fit:
            eff_max = 0

        if eff_max and eff_max > 0:
            mode = "fit"
            fit_max_bytes = eff_max
            parts, num_parts, part_sizes = _fit_parts(
                preamble, files, args.tree, orig_name, eff_max,
            )
            is_byte_mode = True
        else:
            mode = "parts"
            num_parts = min(DEFAULT_NUM_PARTS, len(files))
            parts, part_sizes = _partition_lpt(files, num_parts)

    if num_parts == 1 and mode != "fit":
        print("warning: dump fits in a single part", file=sys.stderr)

    if is_byte_mode:
        print(
            f"Split {orig_name} ({len(files)} files, "
            f"~{total_size_bytes:,} content bytes) into "
            f"{num_parts} part(s) "
            f"[fit under {fit_max_bytes:,} bytes]",
            file=sys.stderr,
        )
    else:
        print(
            f"Split {orig_name} ({len(files)} files, "
            f"~{total_size_lines} lines) into {num_parts} part(s)",
            file=sys.stderr,
        )

    def _report(part_num, n_files, est, out_path):
        pct = 100.0 * est / max(
            (sum(part_sizes) if is_byte_mode else total_size_lines),
            1,
        )
        noun = "file" if n_files == 1 else "files"
        if is_byte_mode:
            print(f"  {out_path.name}: {n_files} {noun}, "
                  f"~{est:,} bytes ({pct:.1f}%)", file=sys.stderr)
        else:
            print(f"  {out_path.name}: {n_files} {noun}, "
                  f"~{est} lines ({pct:.1f}%)", file=sys.stderr)

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
