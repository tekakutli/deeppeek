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

Parser note: dump.py writes each file as a 4-line opener, the file's raw
content, and a closer.  The opener is:

  · a 70-equals header line
  · a "FILE i/N: path" line
  · another 70-equals header line
  · a fence-opener line consisting of three backticks followed by "python"

The closer is: a line of three backticks, then a blank line.

The file's content may itself contain fence lines (markdown, docstrings).
So blocks are located by matching the exact opener, split at the next
opener (or EOF), and the LAST occurrence of "three-backticks + two
newlines" in each slice is trimmed — that is always dump.py's own closer,
regardless of what is inside the content.  Stray `FILE i/N:` markers that
survive inside content are filtered out by a majority vote on N (all real
blocks in one dump share the same N).
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

HEADER = "=" * 70

# Triple-backtick, built at runtime so this file's own markdown fences
# are never broken by its source.
_BT3 = "`" * 3

# Block opener regex — see module docstring.
FILE_BLOCK_START = re.compile(
    r'(?m)^' + re.escape(HEADER) + r'\n'
    r'FILE (\d+)/(\d+): (.+)\n'
    + re.escape(HEADER) + r'\n'
    + _BT3 + r'python\n'
)

# The DIRECTORY TREE section, if present, is:
#   <HEADER>\nDIRECTORY TREE\n<HEADER>\n```\n<tree>\n```\n\n
TREE_BLOCK_RE = re.compile(
    re.escape(HEADER) + r'\nDIRECTORY TREE\n' + re.escape(HEADER) +
    r'\n' + _BT3 + r'\n.*?\n' + _BT3 + r'\n\n',
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_dump(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (preamble, [(path, content), ...]) preserving original order.

    See the module docstring for the robustness strategy.  Normalizes
    CRLF to LF first so the closer check is line-ending-agnostic.
    """
    text = text.replace("\r\n", "\n")

    matches = list(FILE_BLOCK_START.finditer(text))
    if not matches:
        return text, []

    # Majority vote on N — every real block in one dump shares N.
    n_counts = Counter(m.group(2) for m in matches)
    intended_n = n_counts.most_common(1)[0][0]
    kept = [m for m in matches if m.group(2) == intended_n]
    if not kept:
        return text, []

    preamble = text[: kept[0].start()]
    closer = _BT3 + "\n\n"

    files: list[tuple[str, str]] = []
    for idx, m in enumerate(kept):
        start = m.start()
        end = kept[idx + 1].start() if idx + 1 < len(kept) else len(text)
        path = m.group(3)

        # Content starts right after the opener.
        block_content = text[start:end][m.end() - start:]

        # The LAST "```\n\n" is always dump.py's closer.
        pos = block_content.rfind(closer)
        content = block_content[:pos] if pos != -1 else block_content

        files.append((path, content))

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
            fh.write(_BT3 + "python\n")
            fh.write(content)
            if content and not content.endswith("\n"):
                fh.write("\n")
            fh.write(_BT3 + "\n\n")
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
