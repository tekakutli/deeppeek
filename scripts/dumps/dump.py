#!/usr/bin/env python3
"""
Dump files (filtered by extension) into a single markdown file.

Usage:
    dump.py [-a AFFIX] [-r] [-t] [-o PATH] [--exclude-file PATH]... [FILE ...]

If one or more FILE positional arguments are provided, ONLY those files are
dumped (deduplicated, in sorted order). Their paths are interpreted relative
to DEFAULT_DIR (falling back to the current directory). In this mode all
extension / affix / include / exclude filters are bypassed: what you list is
what you get. A directory tree is always rendered, rooted at the common
parent directory of the listed files.

When no FILE arguments are given, the script scans DEFAULT_DIR using the
configured filters below.

TREE MODES
==========
Two tree-rendering modes exist:

  Default (FILE mode)  — the DIRECTORY TREE section shows the subtree
                         that contains the listed files, pruned of any
                         directory that holds no listed file.  Matches
                         the "what's in this dump" question exactly.

  --tree-from-scan     — (only valid with FILE arguments) the DIRECTORY
                         TREE section is built from a full scan of the
                         scan root, showing EVERY file that matches
                         DEFAULT_EXTENSIONS.  Files that are in the
                         explicit list have no marker; files that were
                         filtered out are suffixed with `[skip]`.  This
                         lets a caller (notably dump_filtered.py) show
                         the full project tree with the filtered set
                         highlighted, so the user can see what was
                         excluded without losing the context of what
                         the project contains.

Files whose own content *looks like another dump* — i.e. contains a
`=====...=====\nFILE i/N: ...` block header at line start — are skipped
by default (DEFAULT_SKIP_DUMP_FILES = True). This prevents an
"oh-no, a dump of a dump" situation where re-parsing a generated dump
would pick up the inner FILE markers as if they were top-level files.
Disable with `--no-skip-dump-files` if you really want to include them.

The output filename has two modes:

    · Default (no -o/--output): DEFAULT_OUT with the scanned directory's
      basename appended before the suffix, so scanning ".../myproject"
      writes "project_dump_myproject.md" in the CURRENT directory.

    · -o PATH: write exactly to PATH.  A special value "-" writes the dump
      to stdout (for piping); the "Total lines" summary then goes to stderr,
      so stdout is purely the dump.

CLI flags -a/--affix, -r/--recursive, -t/--tree-only override their
DEFAULT_* counterparts. --exclude-file PATH (repeatable) excludes one
exact file by resolved path — useful when another program (e.g.
dumps_evolve.py) must keep its own inputs out of the scan.
"""

import argparse
import io
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURATION — edit these
# ---------------------------------------------------------------------------
DEFAULT_DIR        = "."              # directory to scan
DEFAULT_OUT        = "project_dump.md" # output markdown file
# Common extensions:
#   .py       Python
#   .sh       Bash / shell
#   .json     JSON
#   .yaml/.yml YAML
#   .service  systemd unit files
#   .md       Markdown
#   .toml     TOML
DEFAULT_EXTENSIONS = [".py", ".sh", "yaml", ".md", ".conf", ".toml", ".js", ".el", ".json"]
DEFAULT_AFFIX      = None             # e.g. "_test" (substring matched in filename)
DEFAULT_RECURSIVE  = False            # descend into subdirectories
DEFAULT_TREE_ONLY  = False            # only emit the directory tree, no file contents

# If True, files whose *own content* contains a `=====...=====\nFILE i/N: `
# block header at line start are skipped.  This is what prevents a stale
# baseline.md / common.md / committed example dump from being folded into
# the current dump — which would later confuse any dump parser (including
# dumps_diff.py / dumps_evolve.py).  Turn off if you actually want to dump
# a file that documents the dump format.
DEFAULT_SKIP_DUMP_FILES = True

# Directories to skip when dumping files. Two kinds of entries are supported:
#   - Plain names (no "/"), e.g. "node_modules": matches a directory with that
#     basename at ANY depth.
#   - Relative paths, e.g. "thisDirectory/thatDirectory": matched against the
#     directory's path RELATIVE TO THE SCANNED DIRECTORY, so only that exact
#     directory (and its subtree) is skipped.
# Directories skipped here still appear in the DIRECTORY TREE, unless they are
# also listed in DEFAULT_TREE_EXCLUDE_DIRS below.
DEFAULT_EXCLUDE_DIRS = [
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "lib", "bin", "recover",
    "kritomatic_xremap", "other_scripts", "playgrounds",
    "data/comfy_panel/history/",
]

# Directories to hide from the DIRECTORY TREE. Same matching rules as
# DEFAULT_EXCLUDE_DIRS. This only affects the tree rendering — it never
# changes which files are dumped.
DEFAULT_TREE_EXCLUDE_DIRS = [".git", "__pycache__", ".venv", "venv", "node_modules",
                             "lib", "bin", "recover",
                             "target", "release",
                             "data/comfy_panel", "other_scripts/room/leader_optimizer",
]

# Directory names whitelist: if NON-EMPTY, only descend into subdirectories
# whose basename is in this list.
DEFAULT_INCLUDE_DIRS = []

# Filename substrings whitelist: if NON-EMPTY, a file is only kept when its
# filename contains AT LEAST ONE of these substrings.
DEFAULT_INCLUDE_FILE_SUBSTRINGS = []

# Filename substrings blacklist: if a filename contains ANY of these, the
# file is skipped.
DEFAULT_EXCLUDE_FILE_SUBSTRINGS = ["project_dump", "shrinking", "tests", 'EMISSION', 'reprint.py']
# ---------------------------------------------------------------------------

HEADER = "=" * 70

# Sentinel key used inside the tree dict to hold the files of a directory.
_FILES = "\0files"

# Detection pattern for "this file is itself a dump": the exact 70-equals
# header followed by a FILE i/N: line.  Anchored at line start.
_DUMP_MARKER_RE = re.compile(
    r"(?m)^" + re.escape(HEADER) + r"\nFILE \d+/\d+: "
)


def normalize_ext(ext: str) -> str | None:
    ext = ext.strip()
    if not ext:
        return None
    if not ext.startswith("."):
        ext = "." + ext
    return ext


def _looks_like_dump(path: Path) -> bool:
    """True if `path`'s content contains a top-level FILE block header.

    Used to skip dumping dumps-of-dumps (stale baseline.md, common.md, a
    committed example showing the format, …).
    """
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return bool(_DUMP_MARKER_RE.search(content))


def dir_matches(rel_dir: Path, name: str, patterns: list[str]) -> bool:
    """Return True if a directory matches one of the exclusion patterns.

    `rel_dir` is the directory's path relative to the scan root, `name` is
    its basename.

    - Patterns containing a path separator (e.g. "a/b") are matched against
      `rel_dir`, i.e. they are relative to the scanned directory.
    - Plain patterns (e.g. "build") match a directory with that basename at
      any depth.
    """
    for raw in patterns:
        pat = raw.strip()
        if not pat:
            continue
        pat_path = Path(pat)
        if len(pat_path.parts) > 1:
            if rel_dir == pat_path:
                return True
        elif name == pat_path.name:
            return True
    return False


def find_files(
    root: Path,
    extensions: list[str],
    affix: str | None,
    exclude_dirs: list[str],
    include_dirs: set[str],
    include_file_substrings: list[str],
    exclude_file_substrings: list[str],
    recursive: bool = True,
    skip_dump_files: bool = True,
) -> list[Path]:
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(os.path.relpath(dirpath, root))

        # prune excluded directories in-place so os.walk skips them
        dirnames[:] = [
            d for d in dirnames
            if not dir_matches(rel_dir / d, d, exclude_dirs)
        ]

        # if a whitelist of directories was provided, only descend into
        # those; empty list = descend into all
        if include_dirs:
            dirnames[:] = [d for d in dirnames if d in include_dirs]

        # if not recursing, prevent os.walk from descending any further
        if not recursive:
            dirnames[:] = []

        for name in filenames:
            if extensions and not any(name.endswith(e) for e in extensions):
                continue
            if affix and affix not in name:
                continue
            if include_file_substrings and not any(
                s in name for s in include_file_substrings
            ):
                continue
            if exclude_file_substrings and any(
                s in name for s in exclude_file_substrings
            ):
                continue
            full = Path(dirpath) / name
            if skip_dump_files and _looks_like_dump(full):
                continue
            results.append(full)

    results.sort()
    return results


def _render_tree(node: dict, prefix: str) -> list[str]:
    lines: list[str] = []
    dirs = sorted(k for k in node if k != _FILES)

    # File entries may be plain strings (legacy tree modes) or
    # (name, is_included) tuples (produced by build_tree with
    # mark_skipped=True).  Normalize to a list of (name, is_included)
    # before rendering.
    raw_files = node.get(_FILES, [])
    file_entries: list[tuple[str, bool]] = []
    for f in raw_files:
        if isinstance(f, tuple):
            file_entries.append(f)
        else:
            file_entries.append((f, True))
    file_entries.sort(key=lambda x: x[0])

    entries: list[tuple[str, bool, bool]] = (
        [(d, True, True) for d in dirs]
        + [(name, False, is_included) for name, is_included in file_entries]
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


def build_tree(
    root: Path,
    include_dirs: set[str],
    tree_exclude_dirs: list[str],
    dumped_files: list[Path],
    mark_skipped: bool = False,
    extensions: list[str] | None = None,
) -> dict:
    """Build a nested dict describing the directory structure under `root`.

    Every directory found under `root` is included — including directories
    that were excluded from dumping — except those matching
    `tree_exclude_dirs`.

    `dumped_files` is the set of files that will actually appear as FILE
    blocks in the dump.

    When `mark_skipped=False` (default, original behaviour), only files in
    `dumped_files` appear in the tree.  This matches what a
    "files I was asked to dump" tree should show.

    When `mark_skipped=True`, every file under `root` whose name matches
    one of `extensions` (if provided) appears in the tree:
      · files in `dumped_files` are added as plain strings
      · files NOT in `dumped_files` are added as `(name, False)` tuples

    `_render_tree` renders the two forms differently, appending `  [skip]`
    to any file marked as not-included.  This is what powers the
    `--tree-from-scan` mode: the tree shows the full candidate set with
    the filtered-out files highlighted, without losing context about what
    the project actually contains.
    """
    dumped = {Path(os.path.relpath(f, root)) for f in dumped_files}
    tree: dict = {}

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(os.path.relpath(dirpath, root))
        rel_parts = () if rel_dir == Path(".") else rel_dir.parts

        dirnames[:] = [
            d for d in dirnames
            if not dir_matches(rel_dir / d, d, tree_exclude_dirs)
        ]

        if include_dirs:
            dirnames[:] = [d for d in dirnames if d in include_dirs]

        node = tree
        for part in rel_parts:
            node = node.setdefault(part, {})

        for name in filenames:
            rel = Path(*rel_parts) / name
            is_included = rel in dumped
            if is_included:
                node.setdefault(_FILES, []).append(
                    (name, True) if mark_skipped else name
                )
            elif mark_skipped:
                # Only show files that would have been candidates under
                # the scan's extension filter.  A `.png` or binary blob
                # that would never be dumped is noise in the tree, not
                # signal about what was excluded.
                if extensions and not any(
                    name.endswith(e) for e in extensions
                ):
                    continue
                node.setdefault(_FILES, []).append((name, False))

    return tree


def build_tree_from_files(
    root: Path,
    files: list[Path],
    tree_exclude_dirs: list[str],
) -> dict:
    """Build a tree dict from an explicit file list.

    Only the directories that lead to one of `files` appear.  Files still
    inside a directory matched by `tree_exclude_dirs` are pruned from the
    tree (but they will still be dumped — this only affects rendering).

    This is the legacy "pruned" tree used when FILE arguments are passed
    without `--tree-from-scan`.  For the "full tree with [skip] markers"
    mode, use `build_tree(..., mark_skipped=True, extensions=...)`.
    """
    tree: dict = {}
    for f in files:
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault(_FILES, []).append(parts[-1])

    def prune(node: dict, rel_dir: Path) -> None:
        for key in list(node.keys()):
            if key == _FILES:
                continue
            sub_rel = rel_dir / key
            if dir_matches(sub_rel, key, tree_exclude_dirs):
                del node[key]
            else:
                prune(node[key], sub_rel)

    prune(tree, Path("."))
    return tree


def render_tree(tree: dict, label: str) -> str:
    """Return a unix-`tree`-style rendering of a tree dict."""
    if label in ("", "."):
        label = "."

    lines = [label + "/"]
    lines.extend(_render_tree(tree, ""))
    return "\n".join(lines)


def resolve_listed_files(file_args: list[str], base_dir: Path) -> list[Path]:
    """Resolve the user-supplied file arguments to absolute, deduplicated,
    sorted Paths. Warnings are printed for arguments that are not files.
    """
    seen: set[Path] = set()
    result: list[Path] = []
    for arg in file_args:
        p = Path(arg)
        if not p.is_absolute():
            p = base_dir / p
        try:
            p = p.resolve()
        except OSError:
            print(f"Warning: cannot resolve {arg!r}, skipped", file=sys.stderr)
            continue
        if not p.is_file():
            print(f"Warning: not a regular file, skipped: {arg}", file=sys.stderr)
            continue
        if p in seen:
            continue
        seen.add(p)
        result.append(p)
    result.sort()
    return result


def output_path_with_dirname(base_out: Path, scanned_dir: Path) -> Path:
    """Append the basename of `scanned_dir` to `base_out` before its suffix."""
    dirname = scanned_dir.name
    if not dirname:  # e.g. scanning filesystem root "/"
        return base_out
    dirname = dirname.replace(os.sep, "_").replace("/", "_")
    return base_out.with_name(f"{base_out.stem}_{dirname}{base_out.suffix}")


def _emit_file_contents(fh, files, display_fn, total):
    """Write `FILE i/N:` blocks for each file into `fh`."""
    for i, f in enumerate(files, 1):
        fh.write(HEADER + "\n")
        fh.write(f"FILE {i}/{total}: {display_fn(f)}\n")
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump files into a markdown file. "
                    "All paths/extensions come from variables in the script; "
                    "CLI overrides exist for affix, recursion, tree-only, "
                    "output path, and per-file exclusions. Pass one or more "
                    "FILE arguments to dump only those files.",
    )
    parser.add_argument(
        "files", nargs="*",
        help=("Optional explicit list of files to dump. When provided, the "
              "directory scan and all filters are bypassed."),
    )
    parser.add_argument(
        "-a", "--affix", default=None,
        help=("Only include files whose filename contains this substring. "
              "Pass an empty string to disable. "
              f"(hardcoded default: {DEFAULT_AFFIX!r})"),
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true", default=None,
        help=("Recurse into subdirectories. Also prepends a directory tree. "
              f"(hardcoded default: {DEFAULT_RECURSIVE})"),
    )
    parser.add_argument(
        "-t", "--tree-only", action="store_true", default=None,
        help=("Only write the directory tree. Implies recursion. "
              f"(hardcoded default: {DEFAULT_TREE_ONLY})"),
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help=("Write the dump to this exact path. \"-\" writes the dump "
              "to stdout, keeping stdout free of anything but the dump. "
              f"(default: {DEFAULT_OUT} with _<scanned-dir> appended)"),
    )
    parser.add_argument(
        "--exclude-file", action="append", default=[], metavar="PATH",
        help=("Exclude this exact file (by resolved path) from the scan. "
              "Repeatable."),
    )
    parser.add_argument(
        "--skip-dump-files", dest="skip_dump_files",
        action="store_true", default=None,
        help=("Skip files whose content looks like another dump (default)."),
    )
    parser.add_argument(
        "--no-skip-dump-files", dest="skip_dump_files",
        action="store_false", default=None,
        help=("Include files whose content looks like another dump."),
    )
    parser.add_argument(
        "--tree-from-scan", action="store_true", default=False,
        help=("(FILE mode only) Build the DIRECTORY TREE section from a "
              "full scan of the scan root instead of the common parent "
              "of the listed files, and mark every file NOT in the "
              "explicit list with `  [skip]`.  Shows the full project "
              "tree with the excluded files highlighted.  No effect "
              "without FILE arguments."),
    )
    args = parser.parse_args()

    root = Path(DEFAULT_DIR)
    out = args.output if args.output else Path(DEFAULT_OUT)
    to_stdout = args.output is not None and str(args.output) == "-"

    extensions = [e for e in (normalize_ext(x) for x in DEFAULT_EXTENSIONS) if e]
    exclude_dirs = [d for d in DEFAULT_EXCLUDE_DIRS if d]
    tree_exclude_dirs = [d for d in DEFAULT_TREE_EXCLUDE_DIRS if d]
    include_dirs = {d for d in DEFAULT_INCLUDE_DIRS if d}
    include_file_substrings = [s for s in DEFAULT_INCLUDE_FILE_SUBSTRINGS if s]
    exclude_file_substrings = [s for s in DEFAULT_EXCLUDE_FILE_SUBSTRINGS if s]
    exclude_paths = {Path(p).resolve() for p in args.exclude_file}
    skip_dump_files = (
        args.skip_dump_files if args.skip_dump_files is not None
        else DEFAULT_SKIP_DUMP_FILES
    )

    tree_only = True if args.tree_only else DEFAULT_TREE_ONLY

    if args.files:
        files = resolve_listed_files(args.files, root)
        if not files:
            print("No valid files provided.", file=sys.stderr)
            return 1

        try:
            common = Path(os.path.commonpath([str(f) for f in files]))
        except ValueError:
            common = files[0].parent
        if common.is_file():
            common = common.parent
        if not common.is_dir():
            common = common.parent

        if args.tree_from_scan:
            # Scan the whole project (from the scan root, not the
            # common parent of the listed files) so the tree shows
            # everything, with `[skip]` on the excluded files.
            tree_root = root.resolve()
        else:
            tree_root = common
        recursive = True
    else:
        if args.tree_from_scan:
            print("warning: --tree-from-scan has no effect without FILE "
                  "arguments; ignoring", file=sys.stderr)

        affix = args.affix if args.affix is not None else DEFAULT_AFFIX
        if affix == "":
            affix = None

        recursive = True if (args.recursive or tree_only) else DEFAULT_RECURSIVE

        files = find_files(
            root,
            extensions,
            affix,
            exclude_dirs,
            include_dirs,
            include_file_substrings,
            exclude_file_substrings,
            recursive,
            skip_dump_files,
        )
        if exclude_paths:
            files = [f for f in files if f.resolve() not in exclude_paths]
        if not files:
            msg = f"No files found in '{root}'"
            if extensions:
                msg += f" with extensions {extensions}"
            if affix:
                msg += f" matching affix '{affix}'"
            if include_file_substrings:
                msg += f" matching any of {include_file_substrings}"
            if include_dirs:
                msg += f" under any of {sorted(include_dirs)}"
            if not recursive:
                msg += " (non-recursive)"
            print(msg, file=sys.stderr)
            return 1
        tree_root = root

    total = len(files)
    scanned_dir = Path(os.path.abspath(str(tree_root)))

    if not args.output:
        out = output_path_with_dirname(out, scanned_dir)

    fh = io.StringIO()
    fh.write(HEADER + "\n")
    fh.write(f"SCANNED DIRECTORY: {scanned_dir}\n")
    fh.write(HEADER + "\n\n")

    if recursive:
        fh.write(HEADER + "\n")
        fh.write("DIRECTORY TREE\n")
        fh.write(HEADER + "\n")
        fh.write("```\n")
        if args.files and not args.tree_from_scan:
            # Legacy FILE mode: pruned tree containing only the
            # listed files' common subtree.
            tree = build_tree_from_files(
                tree_root, files, tree_exclude_dirs,
            )
        else:
            # Scan mode OR --tree-from-scan: walk the whole tree
            # under `tree_root`.  In --tree-from-scan mode, files
            # not in `files` are shown with `[skip]`; in scan mode,
            # every file in `files` is added unmarked (identical to
            # the original behaviour for the scan case).
            tree = build_tree(
                tree_root, include_dirs, tree_exclude_dirs, files,
                mark_skipped=args.tree_from_scan,
                extensions=extensions if args.tree_from_scan else None,
            )
        fh.write(render_tree(tree, str(tree_root)) + "\n")
        fh.write("```\n\n")

    if not tree_only:
        if args.files:
            display_fn = lambda f: os.path.relpath(str(f), str(scanned_dir))
        else:
            def display_fn(f):
                s = str(f)
                return s[2:] if s.startswith("./") else s
        _emit_file_contents(fh, files, display_fn, total)

    content = fh.getvalue()
    line_count = len(content.splitlines()) if content else 0

    if to_stdout:
        sys.stdout.write(content)
        sys.stdout.flush()
        print(f"Total lines in output: {line_count}", file=sys.stderr)
        target = "<stdout>"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        print(f"Total lines in output: {line_count}", file=sys.stdout)
        target = str(out)

    if tree_only:
        print(f"Wrote tree only ({total} file(s)) to {target}",
              file=sys.stderr)
    else:
        print(f"Wrote {total} file(s) to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
