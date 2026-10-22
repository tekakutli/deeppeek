#!/usr/bin/env python3
"""
Compare two dump files produced by dump.py and emit three files:

  1. <dump1>_changed.md   – only the files that differ (dump1's copy)
  2. <dump2>_changed.md   – only the files that differ (dump2's copy)
  3. common.md            – files with identical content in both dumps

The union of the three files has fewer lines than the two inputs summed,
because unchanged files appear once instead of twice.

Usage:
    dumps_diff.py DUMP1 DUMP2 [-o1 OUT1] [-o2 OUT2] [-o3 OUT3] [--keep-tree]
"""

import argparse
import re
import sys
from pathlib import Path

HEADER = "=" * 70

# A file block always starts like:  <HEADER>\nFILE i/N: <path>
FILE_BLOCK_START = re.compile(
    r'(?m)^' + re.escape(HEADER) + r'\nFILE \d+/\d+: '
)
FILE_LINE_RE = re.compile(r'^FILE \d+/\d+: (.*)$')

# The DIRECTORY TREE section, if present, is:
#   <HEADER>\nDIRECTORY TREE\n<HEADER>\n```\n<tree>\n```\n\n
TREE_BLOCK_RE = re.compile(
    re.escape(HEADER) + r'\nDIRECTORY TREE\n' + re.escape(HEADER) +
    r'\n```\n.*?\n```\n\n',
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_block(block: str) -> tuple[str, str]:
    """Parse one 'FILE i/N: path' block into (path, content)."""
    lines = block.split("\n")
    m = FILE_LINE_RE.match(lines[1])
    if not m:
        raise ValueError(f"Malformed FILE line: {lines[1]!r}")
    path = m.group(1)

    # Content is lines[4:-1], but the writer may or may not have appended a
    # trailing newline before the closing fence, so strip trailing blanks
    # then drop the closing ``` line.
    end = len(lines)
    while end > 4 and lines[end - 1] == "":
        end -= 1
    if end > 4 and lines[end - 1] == "```":
        content_lines = lines[4 : end - 1]
    else:
        content_lines = lines[4:end]
    return path, "\n".join(content_lines)


def parse_dump(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (preamble, [(path, content), ...]) preserving original order."""
    starts = [m.start() for m in FILE_BLOCK_START.finditer(text)]
    if not starts:
        return text, []
    preamble = text[: starts[0]]
    files: list[tuple[str, str]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        files.append(parse_block(text[start:end]))
    return preamble, files


def strip_tree(preamble: str) -> str:
    """Remove the DIRECTORY TREE section from a preamble."""
    return TREE_BLOCK_RE.sub("", preamble, count=1)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def write_dump(out_path: Path, title: str, preamble: str,
               files: list[tuple[str, str]]) -> int:
    """Write `files` in dump format (renumbered FILE i/N) and return count."""
    total = len(files)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(HEADER + "\n")
        fh.write(title + "\n")
        fh.write(HEADER + "\n\n")
        fh.write(preamble)

        for i, (path, content) in enumerate(files, 1):
            fh.write(HEADER + "\n")
            fh.write(f"FILE {i}/{total}: {path}\n")
            fh.write(HEADER + "\n")
            fh.write("```python\n")
            fh.write(content)
            if content and not content.endswith("\n"):
                fh.write("\n")
            fh.write("```\n\n")
    return total


def count_lines(path: Path) -> int:
    with open(path, encoding="utf-8") as fh:
        return sum(1 for _ in fh)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare two dump.py outputs and produce "
                    "changed-1, changed-2, and common files."
    )
    ap.add_argument("dump1", type=Path, help="first dump (e.g. commit A)")
    ap.add_argument("dump2", type=Path, help="second dump (e.g. commit B)")
    ap.add_argument("-o1", "--out-changed-1", type=Path, default=None,
                    help="default: <stem1>_changed<suffix>")
    ap.add_argument("-o2", "--out-changed-2", type=Path, default=None,
                    help="default: <stem2>_changed<suffix>")
    ap.add_argument("-o3", "--out-common", type=Path, default=None,
                    help="default: common<suffix>")
    ap.add_argument("--keep-tree", action="store_true",
                    help="Keep the DIRECTORY TREE section (default: strip it).")
    args = ap.parse_args()

    pre1, files1 = parse_dump(args.dump1.read_text(encoding="utf-8"))
    pre2, files2 = parse_dump(args.dump2.read_text(encoding="utf-8"))

    dict1 = dict(files1)
    dict2 = dict(files2)

    # Classify while preserving original order within each dump.
    #   changed1 = files deleted in commit2 OR modified
    #   changed2 = files added in commit2  OR modified
    #   common   = files present in both with identical content
    changed1 = [(p, dict1[p]) for p, _ in files1
                if p not in dict2 or dict2[p] != dict1[p]]
    changed2 = [(p, dict2[p]) for p, _ in files2
                if p not in dict1 or dict1[p] != dict2[p]]
    common = [(p, dict1[p]) for p, _ in files1
              if p in dict2 and dict2[p] == dict1[p]]

    if not args.keep_tree:
        pre1 = strip_tree(pre1)
        pre2 = strip_tree(pre2)

    out1 = args.out_changed_1 or args.dump1.with_name(
        f"{args.dump1.stem}_changed{args.dump1.suffix}")
    out2 = args.out_changed_2 or args.dump2.with_name(
        f"{args.dump2.stem}_changed{args.dump2.suffix}")
    out3 = args.out_common or args.dump1.with_name(
        f"common{args.dump1.suffix}")

    n1 = write_dump(
        out1,
        f"TRIM of {args.dump1.name}: files changed vs {args.dump2.name}",
        pre1, changed1,
    )
    n2 = write_dump(
        out2,
        f"TRIM of {args.dump2.name}: files changed vs {args.dump1.name}",
        pre2, changed2,
    )
    n3 = write_dump(
        out3,
        f"Files IDENTICAL in {args.dump1.name} and {args.dump2.name}",
        pre1, common,
    )

    # Report
    l1, l2, l3 = count_lines(out1), count_lines(out2), count_lines(out3)
    lin1, lin2 = count_lines(args.dump1), count_lines(args.dump2)
    in_total = lin1 + lin2
    out_total = l1 + l2 + l3

    print("Input:")
    print(f"  {args.dump1}: {lin1} lines")
    print(f"  {args.dump2}: {lin2} lines")
    print(f"  total input: {in_total} lines")
    print()
    print("Output:")
    print(f"  {out1}: {l1} lines, {n1} file(s)  [changed in dump1]")
    print(f"  {out2}: {l2} lines, {n2} file(s)  [changed in dump2]")
    print(f"  {out3}: {l3} lines, {n3} file(s)  [common / unchanged]")
    print(f"  total output: {out_total} lines")
    print()
    if in_total:
        red = in_total - out_total
        pct = 100.0 * red / in_total
        print(f"Reduction: {red} lines ({pct:.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
