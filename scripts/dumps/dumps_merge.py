#!/usr/bin/env python3
"""
dumps_merge.py — merge two dumps of the same project into one.

Both dumps must share the same SCANNED DIRECTORY (their absolute root).
The classic case is two dumps produced with dump.py's DEFAULT_SCAN_OFFSET
/ -O option pointing at different subdirectories: each dump scans a
different slice of the project, and dump.py roots both DIRECTORY TREEs at
the same project root so file paths line up.

Merge semantics:

  · File blocks — union of both dumps' FILE blocks, keyed by relative
    path.  Identical content is included once with no warning.  When the
    same path has DIFFERENT content across the two dumps, a warning is
    printed to stderr and the first dump's version is kept (the caller
    is assumed to be merging views of the *same* code state; divergence
    is anomalous and needs human eyes).

  · Directory tree — recursively merged.
        Files are included iff either side included them.
        A `  [skip]` marker survives only when BOTH sides marked the
        file `[skip]` — that is, the file lies outside both slices.

    With modern dump.py this is effectively a plain union: offset mode
    emits a *pruned* tree (only dumped files, no `[skip]` markers), so
    every file entry carries `included=True` on both sides.  The `[skip]`
    logic is retained for compatibility with `--tree-from-scan` dumps,
    which do emit `[skip]` for files outside the explicit list.

  · Header — the SCANNED DIRECTORY root is preserved, annotated with
    `(merged: <a> + <b>)` describing provenance.  The annotation is
    stripped again by SCANNED_DIR_RE, so merged dumps can be re-merged
    or diffed alongside their inputs without confusing tooling.

Usage:

    dumps_merge.py DUMP1 DUMP2 [-o OUT]

    # typical offset workflow:
    dump.py -O kritomatic_daemon/panel/foo  -o dump_foo.md
    dump.py -O kritomatic_daemon/panel/bar  -o dump_bar.md
    dumps_merge.py dump_foo.md dump_bar.md

    # default output: ./project_dump_merged.md (cwd, overwritten on
    # every run — never derived from the input filenames, so re-merging
    # always lands at the same predictable path).

Exit codes:
    0  merged successfully
    1  hard error (unreadable input, malformed dump, missing header, …)
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path


HEADER = "=" * 70
_BT3 = "`" * 3
_FILES = "\0files"

# Fixed default output — deliberately NOT derived from dump1's name or
# location, so successive merges always overwrite the same file rather
# than accumulating _merged_merged_merged.md artifacts.
DEFAULT_OUT = Path("project_dump_merged.md")

# Same opener regex dumps_diff.py uses.
FILE_BLOCK_START = re.compile(
    r'(?m)^' + re.escape(HEADER) + r'\n'
    r'FILE (\d+)/(\d+): (.+)\n'
    + re.escape(HEADER) + r'\n'
    + _BT3 + r'python\n'
)

# DIRECTORY TREE section: <HEADER>\nDIRECTORY TREE\n<HEADER>\n```\n...\n```\n
TREE_BLOCK_RE = re.compile(
    re.escape(HEADER) + r'\nDIRECTORY TREE\n' + re.escape(HEADER) +
    r'\n' + _BT3 + r'\n(.*?)\n' + _BT3 + r'\n',
    re.DOTALL,
)

# SCANNED DIRECTORY line.  The `(.+?)` is non-greedy so an optional
# trailing `(scan offset: …)` or `(merged: …)` annotation is stripped;
# paths containing parentheses mid-string still parse correctly because
# the suffix pattern requires `\s+\(...\)\s*$` to fit exactly.
SCANNED_DIR_RE = re.compile(
    r'^SCANNED DIRECTORY:\s*(.+?)(?:\s+\([^)]*\))?\s*$',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_dump(text):
    """Return (preamble, [(path, content), ...]).

    Block split matches dumps_diff.py's robustness strategy:
      · locate openers by exact match
      · majority-vote on N (all real blocks in a dump share N)
      · split at the next opener (or EOF)
      · trim the LAST ```\\n\\n — always dump.py's closer, even if the
        file content contains fence-looking lines.
    """
    text = text.replace("\r\n", "\n")
    matches = list(FILE_BLOCK_START.finditer(text))
    if not matches:
        return text, []

    n_counts = Counter(m.group(2) for m in matches)
    intended_n = n_counts.most_common(1)[0][0]
    kept = [m for m in matches if m.group(2) == intended_n]
    if not kept:
        return text, []

    preamble = text[: kept[0].start()]
    closer = _BT3 + "\n\n"
    files = []
    for idx, m in enumerate(kept):
        start = m.start()
        end = kept[idx + 1].start() if idx + 1 < len(kept) else len(text)
        path = m.group(3)
        block_content = text[start:end][m.end() - start:]
        pos = block_content.rfind(closer)
        content = block_content[:pos] if pos != -1 else block_content
        files.append((path, content))
    return preamble, files


def extract_scanned_dir(preamble):
    m = SCANNED_DIR_RE.search(preamble)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Tree parsing
# ---------------------------------------------------------------------------

def _parse_tree_line(line):
    """Parse one rendered-tree line -> (depth, is_dir, name, included).

    Prefix chars are `│` / ` ` (four per level).  Connectors are
    `├── ` / `└── ` (four chars).  `  [skip]` is exactly 8 chars.
    """
    i = 0
    while i < len(line) and line[i] in "│ ":
        i += 1
    if i % 4 != 0:
        return None
    depth = i // 4
    if line[i:i + 4] not in ("├── ", "└── "):
        return None
    rest = line[i + 4:]
    included = True
    if rest.endswith("  [skip]"):
        rest = rest[:-8]
        included = False
    if rest.endswith("/"):
        return (depth, True, rest[:-1], True)
    return (depth, False, rest, included)


def _parse_rendered_tree(text):
    """Invert dump.py's renderer: rendered lines -> nested dict."""
    if not text:
        return {}
    lines = text.split("\n")
    root = {}
    stack = [root]
    # lines[0] is the label ("/path/"); skip it.
    for line in lines[1:]:
        if not line:
            continue
        info = _parse_tree_line(line)
        if info is None:
            continue
        depth, is_dir, name, included = info
        while len(stack) > depth + 1:
            stack.pop()
        node = stack[-1]
        if is_dir:
            if name not in node:
                node[name] = {}
            stack.append(node[name])
        else:
            node.setdefault(_FILES, []).append((name, included))
    return root


def parse_tree(preamble):
    m = TREE_BLOCK_RE.search(preamble)
    if not m:
        return None
    return _parse_rendered_tree(m.group(1))


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def merge_files(files1, files2):
    """Union by relative path.

    Returns (merged, conflicts): merged is [(path, content)] sorted by
    path; conflicts is the list of paths whose content differs between
    the two inputs (the first dump's version wins for those).
    """
    d1 = dict(files1)
    d2 = dict(files2)
    merged = []
    conflicts = []
    for p in sorted(set(d1) | set(d2)):
        in1 = p in d1
        in2 = p in d2
        if in1 and in2:
            if d1[p] == d2[p]:
                merged.append((p, d1[p]))
            else:
                conflicts.append(p)
                merged.append((p, d1[p]))
        elif in1:
            merged.append((p, d1[p]))
        else:
            merged.append((p, d2[p]))
    return merged, conflicts


def merge_trees(t1, t2):
    """Recursively merge two tree dicts.

    Under modern dump.py, both inputs are pruned trees (offset mode) and
    every file entry has `included=True`; this therefore reduces to a
    plain union of files and directories.  The `or` logic is retained
    for `--tree-from-scan` dumps, which emit `included=False` (rendered
    as `  [skip]`) for files outside the explicit list: a file is
    skipped in the result only if it was skipped in BOTH inputs.
    """
    if not t1 and not t2:
        return {}
    t1 = t1 or {}
    t2 = t2 or {}

    result = {}

    files_by_name = {}
    for name, inc in t1.get(_FILES, []):
        files_by_name[name] = [inc, False]
    for name, inc in t2.get(_FILES, []):
        if name in files_by_name:
            files_by_name[name][1] = inc
        else:
            files_by_name[name] = [False, inc]
    if files_by_name:
        result[_FILES] = [
            (name, a or b) for name, (a, b) in files_by_name.items()
        ]

    all_dirs = (set(k for k in t1 if k != _FILES)
                | set(k for k in t2 if k != _FILES))
    for d in all_dirs:
        result[d] = merge_trees(t1.get(d), t2.get(d))

    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_tree(node, prefix):
    """Render one node of the tree dict — same shape dump.py produces."""
    lines = []
    dirs = sorted(k for k in node if k != _FILES)

    file_entries = list(node.get(_FILES, []))
    file_entries.sort(key=lambda x: x[0])

    entries = (
        [(d, True, True) for d in dirs]
        + [(name, False, inc) for name, inc in file_entries]
    )

    for i, (name, is_dir, is_included) in enumerate(entries):
        last = i == len(entries) - 1
        connector = "└── " if last else "├── "
        if is_dir:
            lines.append(prefix + connector + name + "/")
            extension = "    " if last else "│   "
            lines.extend(_render_tree(node[name], prefix + extension))
        else:
            suffix = "" if is_included else "  [skip]"
            lines.append(prefix + connector + name + suffix)
    return lines


def render_tree(tree, label):
    if label in ("", "."):
        label = "."
    lines = [label + "/"]
    lines.extend(_render_tree(tree, ""))
    return "\n".join(lines)


def _count_skip_markers(tree):
    """Count `included=False` entries in a tree dict (for diagnostics)."""
    n = 0
    for key, val in tree.items():
        if key == _FILES:
            n += sum(1 for _, inc in val if not inc)
        else:
            n += _count_skip_markers(val)
    return n


def write_dump(out_fh, scanned_dir, annotation, tree, files):
    """Write the merged dump in dump.py-compatible format."""
    out_fh.write(HEADER + "\n")
    header_line = f"SCANNED DIRECTORY: {scanned_dir}"
    if annotation:
        header_line += f" {annotation}"
    out_fh.write(header_line + "\n")
    out_fh.write(HEADER + "\n\n")

    if tree is not None:
        out_fh.write(HEADER + "\n")
        out_fh.write("DIRECTORY TREE\n")
        out_fh.write(HEADER + "\n")
        out_fh.write(_BT3 + "\n")
        out_fh.write(render_tree(tree, scanned_dir) + "\n")
        out_fh.write(_BT3 + "\n\n")

    total = len(files)
    for i, (path, content) in enumerate(files, 1):
        out_fh.write(HEADER + "\n")
        out_fh.write(f"FILE {i}/{total}: {path}\n")
        out_fh.write(HEADER + "\n")
        out_fh.write(_BT3 + "python\n")
        out_fh.write(content)
        if content and not content.endswith("\n"):
            out_fh.write("\n")
        out_fh.write(_BT3 + "\n\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dumps_merge.py",
        description=(
            "Merge two dumps of the same project (typically made with "
            "different DEFAULT_SCAN_OFFSET values) into one combined "
            "dump.  Warns on any file whose content differs between "
            "the inputs."
        ),
    )
    ap.add_argument("dump1", type=Path, help="first dump")
    ap.add_argument("dump2", type=Path, help="second dump")
    ap.add_argument(
        "-o", "--output", type=Path, default=None,
        help=f"output path.  Default: {DEFAULT_OUT} in the current "
             f"working directory.  Use \"-\" for stdout.",
    )
    args = ap.parse_args(argv)

    for p in (args.dump1, args.dump2):
        if not p.is_file():
            print(f"error: not a file: {p}", file=sys.stderr)
            return 1

    try:
        text1 = args.dump1.read_text(encoding="utf-8")
        text2 = args.dump2.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read input: {e}", file=sys.stderr)
        return 1

    pre1, files1 = parse_dump(text1)
    pre2, files2 = parse_dump(text2)

    if not files1:
        print(f"error: {args.dump1} contains no FILE blocks",
              file=sys.stderr)
        return 1
    if not files2:
        print(f"error: {args.dump2} contains no FILE blocks",
              file=sys.stderr)
        return 1

    root1 = extract_scanned_dir(pre1)
    root2 = extract_scanned_dir(pre2)
    if root1 is None:
        print(f"error: {args.dump1} has no SCANNED DIRECTORY line",
              file=sys.stderr)
        return 1
    if root2 is None:
        print(f"error: {args.dump2} has no SCANNED DIRECTORY line",
              file=sys.stderr)
        return 1
    if root1 != root2:
        print(
            "warning: the two dumps have different SCANNED DIRECTORY "
            "roots:\n"
            f"  {args.dump1}: {root1}\n"
            f"  {args.dump2}: {root2}\n"
            "  proceeding, using the first as canonical — file paths "
            "may not line up!",
            file=sys.stderr,
        )
    scanned_dir = root1

    merged_files, conflicts = merge_files(files1, files2)

    if conflicts:
        print(
            f"warning: {len(conflicts)} file(s) appear in both dumps "
            f"with DIFFERENT content; keeping {args.dump1.name}'s "
            f"version:",
            file=sys.stderr,
        )
        for p in conflicts:
            print(f"  · {p}", file=sys.stderr)

    tree1 = parse_tree(pre1)
    tree2 = parse_tree(pre2)
    if tree1 is not None or tree2 is not None:
        merged_tree = merge_trees(tree1, tree2)
        # Sanity note: offset-mode trees are pruned and contain no
        # `[skip]` markers; `--tree-from-scan` trees do.  If the merged
        # result still carries any, mention it so the caller knows what
        # they're looking at.
        n_skip = _count_skip_markers(merged_tree)
        if n_skip:
            print(
                f"note: merged tree retains {n_skip} `[skip]` marker(s) "
                f"(files marked skipped in BOTH inputs — only possible "
                f"with --tree-from-scan dumps)",
                file=sys.stderr,
            )
    else:
        merged_tree = None

    annotation = f"(merged: {args.dump1.name} + {args.dump2.name})"

    # Fixed default: ./project_dump_merged.md, overwritten every run.
    # Never derived from dump1's name or location.
    if args.output is not None:
        out_path = args.output
        to_stdout = str(out_path) == "-"
    else:
        out_path = DEFAULT_OUT
        to_stdout = False

    if to_stdout:
        write_dump(sys.stdout, scanned_dir, annotation,
                   merged_tree, merged_files)
        target = "<stdout>"
    else:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fh:
                write_dump(fh, scanned_dir, annotation,
                           merged_tree, merged_files)
        except OSError as e:
            print(f"error: cannot write {out_path}: {e}", file=sys.stderr)
            return 1
        target = str(out_path)

    summary = (
        f"Merged {len(files1)} + {len(files2)} file(s) "
        f"-> {len(merged_files)} unique"
    )
    if conflicts:
        summary += f" ({len(conflicts)} conflict(s))"
    print(f"{summary}; wrote to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
