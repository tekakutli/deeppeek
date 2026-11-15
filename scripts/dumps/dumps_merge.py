#!/usr/bin/env python3
"""
dumps_merge.py — merge two or more dumps of the same project into one.

All dumps must share the same SCANNED DIRECTORY (their absolute root).
The classic case is several dumps produced with dump.py's
DEFAULT_SCAN_OFFSET / -O option pointing at different subdirectories:
each dump scans a different slice of the project, and dump.py roots all
DIRECTORY TREEs at the same project root so file paths line up.

Merging is applied left-to-right and is exactly equivalent to running
dumps_merge.py pairwise on the running result and the next input:

    dumps_merge.py a b -o tmp1
    dumps_merge.py tmp1 c -o tmp2
    dumps_merge.py tmp2 d -o out

The only observable difference is the SCANNED DIRECTORY annotation,
which lists every input rather than only the last pair.  Annotations
are stripped on parse (see SCANNED_DIR_RE), so re-merging a merged
dump behaves identically either way.

Merge semantics (applied cumulatively):

  · File blocks — union of all dumps' FILE blocks, keyed by relative
    path.  Identical content is included once with no warning.  When
    the same path has DIFFERENT content across dumps, a warning is
    printed to stderr and the FIRST dump's version is kept (the caller
    is assumed to be merging views of the *same* code state; divergence
    is anomalous and needs human eyes).

  · Directory tree — recursively merged.
        Files are included iff any dump included them.
        A `  [skip]` marker survives only when ALL dumps marked the
        file `[skip]` — that is, the file lies outside every slice.

    With modern dump.py this is effectively a plain union: offset mode
    emits a *pruned* tree (only dumped files, no `[skip]` markers), so
    every file entry carries `included=True` on all sides.  The `[skip]`
    logic is retained for compatibility with `--tree-from-scan` dumps,
    which do emit `[skip]` for files outside the explicit list.

  · Header — the SCANNED DIRECTORY root is preserved, annotated with
    `(merged: <a> + <b> + …)` describing provenance.  The annotation is
    stripped again by SCANNED_DIR_RE, so merged dumps can be re-merged
    or diffed alongside their inputs without confusing tooling.

Usage:

    dumps_merge.py DUMP1 DUMP2 [DUMP3 ...] [-o OUT]

    # typical offset workflow:
    dump.py -O kritomatic_daemon/panel/foo  -o dump_foo.md
    dump.py -O kritomatic_daemon/panel/bar  -o dump_bar.md
    dump.py -O kritomatic_daemon/panel/baz  -o dump_baz.md
    dumps_merge.py dump_foo.md dump_bar.md dump_baz.md
    # -> ./project_dump_kritomatic_daemon_merged.md (or whatever the
    #    common root's basename is), if all three dumps agree on their
    #    SCANNED DIRECTORY root

    # default output: ./project_dump_<root>_merged.md where <root> is
    # the common SCANNED DIRECTORY basename.  Written to cwd and
    # overwritten on every run — never derived from the input dump
    # filenames, so re-merging always lands at the same predictable
    # path.  Falls back to ./project_dump_merged.md (i.e. the same
    # template with the <root> slot empty) when the inputs disagree on
    # the root (a warning is emitted in that case) or the root has no
    # usable basename (e.g. "/").

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

# Default output is `project_dump_<root>_merged.md` (or, without a
# usable <root>, `project_dump_merged.md` — same template, empty slot).
# The name is a property of the *project*, not of the input dump
# filenames, so successive merges always overwrite the same predictable
# file rather than accumulating _merged_merged_merged.md artifacts.
DEFAULT_OUT_TEMPLATE = "project_dump_{root}merged.md"


def default_output_path(scanned_dir):
    """project_dump_<root-basename>_merged.md, or the unsuffixed form."""
    name = Path(scanned_dir).name
    if not name or name in (".", ".."):
        root = ""
    else:
        root = f"{name}_"
    return Path(DEFAULT_OUT_TEMPLATE.format(root=root))


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
            "Merge two or more dumps of the same project (typically made "
            "with different DEFAULT_SCAN_OFFSET values) into one combined "
            "dump.  Merging is applied left-to-right, exactly as if you "
            "had chained pairwise merges of the running result and the "
            "next input.  Warns on any file whose content differs across "
            "the inputs."
        ),
    )
    ap.add_argument(
        "dumps", nargs="+", type=Path, metavar="DUMP",
        help="two or more dumps to merge, in order",
    )
    ap.add_argument(
        "-o", "--output", type=Path, default=None,
        help=(
            "output path.  Default: project_dump_<root>_merged.md in the "
            "current working directory, where <root> is the common "
            "SCANNED DIRECTORY basename.  Falls back to "
            "project_dump_merged.md (the same name with no <root>) when "
            "the inputs disagree on the root (a warning is printed) or "
            "the root has no usable basename.  Use \"-\" for stdout."
        ),
    )
    args = ap.parse_args(argv)

    if len(args.dumps) < 2:
        ap.error("at least two dumps are required")

    for p in args.dumps:
        if not p.is_file():
            print(f"error: not a file: {p}", file=sys.stderr)
            return 1

    texts = []
    for p in args.dumps:
        try:
            texts.append(p.read_text(encoding="utf-8"))
        except OSError as e:
            print(f"error: cannot read input {p}: {e}", file=sys.stderr)
            return 1

    parsed = [parse_dump(t) for t in texts]
    preambles = [pr for pr, _ in parsed]
    file_lists = [fl for _, fl in parsed]

    for p, files in zip(args.dumps, file_lists):
        if not files:
            print(f"error: {p} contains no FILE blocks", file=sys.stderr)
            return 1

    roots = [extract_scanned_dir(pr) for pr in preambles]
    for p, root in zip(args.dumps, roots):
        if root is None:
            print(f"error: {p} has no SCANNED DIRECTORY line",
                  file=sys.stderr)
            return 1

    scanned_dir = roots[0]
    roots_agree = True
    for i in range(1, len(args.dumps)):
        if roots[i] != scanned_dir:
            roots_agree = False
            print(
                "warning: the two dumps have different SCANNED DIRECTORY "
                "roots:\n"
                f"  {args.dumps[0]}: {scanned_dir}\n"
                f"  {args.dumps[i]}: {roots[i]}\n"
                "  proceeding, using the first as canonical — file paths "
                "may not line up!",
                file=sys.stderr,
            )

    # Fold merge_files left-to-right.  Each step keeps the left side's
    # version on conflict — which, by induction, is always the FIRST
    # dump's version — so this matches sequential application exactly.
    # Union of per-step conflicts is the set of paths any pair of dumps
    # disagreed on.
    merged_files = list(file_lists[0])
    all_conflicts = set()
    for i in range(1, len(file_lists)):
        merged_files, step_conflicts = merge_files(
            merged_files, file_lists[i]
        )
        all_conflicts.update(step_conflicts)

    if all_conflicts:
        print(
            f"warning: {len(all_conflicts)} file(s) appear in multiple "
            f"dumps with DIFFERENT content; keeping {args.dumps[0].name}'s "
            f"version:",
            file=sys.stderr,
        )
        for p in sorted(all_conflicts):
            print(f"  · {p}", file=sys.stderr)

    # Fold merge_trees left-to-right.  `included = a or b` composes to
    # "included iff any dump included it", i.e. `[skip]` survives only
    # when ALL dumps marked it skipped.
    trees = [parse_tree(pr) for pr in preambles]
    merged_tree = None
    for t in trees:
        if t is None:
            continue
        if merged_tree is None:
            merged_tree = t
        else:
            merged_tree = merge_trees(merged_tree, t)

    if merged_tree is not None:
        n_skip = _count_skip_markers(merged_tree)
        if n_skip:
            print(
                f"note: merged tree retains {n_skip} `[skip]` marker(s) "
                f"(files marked skipped in ALL inputs — only possible "
                f"with --tree-from-scan dumps)",
                file=sys.stderr,
            )

    annotation = (
        "(merged: " + " + ".join(p.name for p in args.dumps) + ")"
    )

    # Default output: project_dump_<root>_merged.md in cwd when all
    # inputs agree on the SCANNED DIRECTORY root, else the same name
    # with the <root> slot empty.  Either way the name is derived from
    # the project (or nothing), never from the input dump filenames —
    # successive merges overwrite the same predictable file.
    if args.output is not None:
        out_path = args.output
        to_stdout = str(out_path) == "-"
    else:
        out_path = (
            default_output_path(scanned_dir)
            if roots_agree
            else default_output_path("")
        )
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

    counts = " + ".join(str(len(fl)) for fl in file_lists)
    summary = f"Merged {counts} file(s) -> {len(merged_files)} unique"
    if all_conflicts:
        summary += f" ({len(all_conflicts)} conflict(s))"
    print(f"{summary}; wrote to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
