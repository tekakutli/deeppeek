#!/usr/bin/env python3
"""merge_md.py — merge input files into a single markdown file.

Usage:
    merge_md.py [-o OUT] FILE [FILE ...]
    merge_md.py [-o OUT] --group NAME FILE [FILE ...] [--group NAME2 …]

Each file is rendered as one block:

    ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    MERGED FILE i/N: <path>
    ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    <fence><language>
    <content>
    <fence>

Why `MERGED FILE` and `+`, not `FILE` and `=`
=============================================

The most common merge inputs are *themselves dump files* (from
`dump.py`), which already use:

    ======================================================================
    FILE 1/294: some/file.py
    ======================================================================

If `merge_md.py` used the same pattern, a reader scanning the merged
file would see two layers of `FILE i/N:` headers and `===` separators —
indistinguishable.  The merge header uses:

  · the word `MERGED` before `FILE`
  · a `+` separator (never `=`)
  · a fixed-width `+` run of 70 characters (or wider if the file's own
    content contains a longer `+` run)

so a reader can tell at a glance: anything starting with `+` and
`MERGED FILE` is a real boundary; anything with `FILE i/N:` or `===`
is dump-file *content* inside a fenced block.

Grouped output
==============

Pass `--group NAME FILE [FILE ...]` (repeatable) to bundle files into
named groups.  Each group gets a `#`-bounded heading; files inside a
group still have their own `+`/`MERGED FILE` header.  `#` is visually
distinct from both `+` (file separator) and `=` (dump separator), so
the three levels — dump content, file boundary, group boundary — are
each unambiguous.  Without `--group`, files render flat as before.

The preamble of a grouped merge contains a tree view:

    send_tests_merged.md
    ├── test log/  (1 file(s))
    │   └── tests.log
    ├── Test files/  (1 file(s))
    │   └── test_recover.py
    ├── 13_scoped_add_verb (from test_recover.py)/  (3 file(s))
    │   ├── fixtures/13_scoped_add_verb.md
    │   ├── expected/13_scoped_add_verb/view.js
    │   └── preexisting/13_scoped_add_verb/view.js
    └── …

Each group's files are stripped to their longest common parent
prefix so the tree stays readable — three files under
`<root>/tests/recover/{fixtures,expected,preexisting}/13_…/`
show as `fixtures/13_…md`, `expected/13_…/view.js`,
`preexisting/13_…/view.js`, not three full absolute paths.

The fence-width guarantee
=========================

The fence around each file's content is a run of backticks whose width
strictly exceeds any backtick run in *that file's own content*.  So no
line of the file can close the fence early, and the reader can always
identify the real boundary as the widest matching pair: whatever the
file contains — nested fences, `====` lines, another `FILE i/N:`
header — its own copy of a marker is strictly narrower than the outer
boundary that wraps it.

Options:
    -o, --output PATH    where to write the merged file.  Default: an
                         auto-named merged_<timestamp>.md in the
                         current directory.  Pass '-' to write to
                         stdout.
    -t, --title TEXT     a title heading to put at the top.
    --lang LANG          force this language tag for every file's
                         fence.  Default: derived from each file's
                         extension.
    --no-preamble        omit the format-explanation preamble and file
                         list at the top.
    --no-language-tag    omit the language tag from the fence opener.
    --group NAME FILE... open a named group; repeatable.  Files
                         inside the group have their own `MERGED FILE`
                         header; the group itself gets a `#`-bounded
                         heading.  Mutually exclusive with positional
                         FILE args.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path


# Extension -> markdown fence language tag.  Everything else -> "text".
_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "tsx",
    ".md": "markdown", ".markdown": "markdown",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish",
    ".rs": "rust", ".go": "go", ".rb": "ruby", ".php": "php",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".groovy": "groovy",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".m": "objective-c", ".swift": "swift", ".dart": "dart",
    ".lua": "lua", ".pl": "perl", ".pm": "perl", ".r": "r",
    ".sql": "sql",
    ".graphql": "graphql", ".gql": "graphql",
    ".proto": "protobuf",
    ".vue": "vue", ".svelte": "svelte",
    ".xml": "xml", ".svg": "xml",
    ".csv": "csv",
    ".txt": "text", ".log": "text",
}

# The two characters used for boundaries.  `+` is deliberately different
# from dump.py's `=`: dump files that get merged as inputs look identical
# to `FILE i/N:` / `===` at the content level, and using the same
# characters for the merge boundary would be ambiguous.
_SEP_CHAR = "+"
_GROUP_CHAR = "#"
_MIN_SEPARATOR_WIDTH = 70
_MIN_FENCE_WIDTH = 3


def _max_run(text: str, ch: str) -> int:
    """Length of the longest consecutive run of `ch` in `text`."""
    if not text:
        return 0
    longest = 0
    current = 0
    for c in text:
        if c == ch:
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest


def _separator_for(content: str) -> str:
    """A run of '+' whose width strictly exceeds any '+' run in the
    file's own content, so the separator cannot appear verbatim in
    the file body."""
    return _SEP_CHAR * max(_MIN_SEPARATOR_WIDTH,
                            _max_run(content, _SEP_CHAR) + 1)


def _fence_for(content: str) -> str:
    """A run of backticks whose width strictly exceeds any backtick
    run in the file's own content, so the fence cannot be closed early
    by something inside the file."""
    return "`" * max(_MIN_FENCE_WIDTH, _max_run(content, "`") + 1)


def _language_for(path: Path, override: str | None) -> str:
    if override is not None:
        return override
    return _LANG_BY_EXT.get(path.suffix.lower(), "text")


def _read_file(path: Path) -> str:
    """Read as UTF-8; fall back to latin-1 for byte-preserving
    behaviour on non-UTF-8 files."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _group_header(name: str, n_files: int) -> str:
    """`#`-bounded group heading.  `#` is distinct from the `+` file
    separator and the `=` dump separator, so all three levels are
    unambiguous."""
    bar = _GROUP_CHAR * _MIN_SEPARATOR_WIDTH
    return (
        f"{bar}\n"
        f"# GROUP: {name}\n"
        f"# {n_files} file(s)\n"
        f"{bar}\n"
        f"\n"
    )


def _common_prefix_dir(paths: list[Path]) -> Path:
    """Longest common *parent-directory* prefix across `paths`.

    Used by the tree renderer so a group of three files under
    `<root>/tests/recover/{fixtures,expected,preexisting}/13_…/`
    shows as `fixtures/13_…md`, `expected/13_…/view.js`,
    `preexisting/13_…/view.js` — not three full absolute paths.

    Returns `Path("")` when the input is empty or has no shared
    prefix (e.g. files on different drives, or a single file whose
    parent is relative).  Callers fall back to the raw path in that
    case.
    """
    if not paths:
        return Path("")
    parents = [p.parent.parts for p in paths]
    if not parents:
        return Path("")
    common: list[str] = []
    for parts in zip(*parents):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    if not common:
        return Path("")
    return Path(*common)


def _render_group_tree(
    groups: list[tuple[str | None, list[Path]]],
    root_label: str = "merged",
) -> str:
    """Render groups + their files as a unix `tree`-style block.

    Groups become directories; files become leaf nodes; each group's
    file paths are stripped to their longest common parent prefix so
    the tree stays readable.  Ungrouped (flat-mode) entries render
    under a single `(flat)/` directory.

    Preserves input order — `send_tests` deliberately emits
    fixtures-first, then expected, then preexisting, and re-sorting
    would lose that grouping hint.
    """
    lines = [root_label + "/"]
    if not groups:
        return lines[0]

    for gi, (name, files) in enumerate(groups):
        last_group = gi == len(groups) - 1
        group_connector = "└── " if last_group else "├── "
        group_ext = "    " if last_group else "│   "
        header = name if name is not None else "(flat)"
        lines.append(
            f"{group_connector}{header}/  ({len(files)} file(s))"
        )

        common = _common_prefix_dir(files)
        for fi, f in enumerate(files):
            last_file = fi == len(files) - 1
            file_connector = "└── " if last_file else "├── "
            try:
                rel = (f.relative_to(common)
                       if common != Path("") else f)
            except ValueError:
                rel = f
            lines.append(f"{group_ext}{file_connector}{rel}")
    return "\n".join(lines)


def _emit_block(fh, index: int, total: int, path: Path,
                content: str, language: str, *,
                emit_lang_tag: bool) -> None:
    sep = _separator_for(content)
    fence = _fence_for(content)
    opener = fence + (language if emit_lang_tag and language else "")

    fh.write(sep + "\n")
    fh.write(f"MERGED FILE {index}/{total}: {path}\n")
    fh.write(sep + "\n")
    fh.write(opener + "\n")
    fh.write(content)
    if content and not content.endswith("\n"):
        fh.write("\n")
    fh.write(fence + "\n\n")


def _preamble(total: int, files: list[Path],
              groups: list[tuple[str | None, list[Path]]] | None = None,
              has_groups: bool = False,
              root_label: str = "merged") -> str:
    lines: list[str] = ["# Merged file", ""]
    if has_groups and groups:
        named_count = sum(1 for n, _ in groups if n is not None)
        lines.append(
            f"{total} file(s) across {named_count} group(s):"
        )
        lines.append("")
        tree = _render_group_tree(groups, root_label)
        tree_fence = _fence_for(tree)
        lines.append(tree_fence)
        lines.append(tree)
        lines.append(tree_fence)
        lines.append("")
    else:
        lines.append(f"{total} file(s) merged:")
        lines.append("")
        for i, p in enumerate(files, 1):
            lines.append(f"{i}. {p}")
        lines.append("")

    lines += [
        "Each file block has this structure:",
        "",
        "    " + _SEP_CHAR * _MIN_SEPARATOR_WIDTH,
        "    MERGED FILE i/N: <path>",
        "    " + _SEP_CHAR * _MIN_SEPARATOR_WIDTH,
        "    <fence><language>",
        "    <content>",
        "    <fence>",
        "",
        "Both the `+` separator and the fence are drawn *wider* than any",
        "same-character run in that file's own content. So a reader can",
        "always identify the real boundary as the widest matching pair:",
        "whatever the file contains — nested fences of any width, `====`",
        "lines, another `FILE i/N:` header — its own copy of a marker is",
        "strictly narrower than the outer boundary that wraps it.",
        "",
    ]
    if has_groups:
        lines += [
            "Files may be organized into groups. Each group is preceded",
            "by a `#`-bounded heading:",
            "",
            "    " + _GROUP_CHAR * _MIN_SEPARATOR_WIDTH,
            "    # GROUP: <group name>",
            "    # <n> file(s)",
            "    " + _GROUP_CHAR * _MIN_SEPARATOR_WIDTH,
            "",
            "`#` is distinct from both `+` (file separators) and `=`",
            "(dump separators used by `dump.py`), so group boundaries,",
            "file boundaries, and dump boundaries are each unambiguous.",
            "",
        ]
    lines += [
        "**Input files that are themselves dump files.** A common case",
        "for `merge_md.py` is merging `project_dump_*.md` files, which",
        "internally use `FILE i/N:` headers and `===` separators. Those",
        "are *content*: they appear inside a fenced block, and the fence",
        "around them is wider than any backtick run they contain.",
        "",
        "Real boundaries in this merged file always use the word `MERGED`",
        "before `FILE`, and a `+` separator, never `FILE` alone and never",
        "`===`. So a line beginning with `FILE ` or consisting of `===`",
        "is unambiguously a dump's content, not a merge boundary.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="merge_md.py",
        description=(
            "Merge input files into a single markdown file with "
            "visually unambiguous per-file boundaries."
        ),
    )
    parser.add_argument(
        "files", nargs="*", type=Path, metavar="FILE",
        help="files to merge, in input order (positional mode; "
             "mutually exclusive with --group)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="write the merged output to PATH.  Default: an auto-named "
             "merged_<timestamp>.md in the current directory.  Pass '-' "
             "to write to stdout.",
    )
    parser.add_argument(
        "-t", "--title", default=None, metavar="TEXT",
        help="a title heading to put at the top of the merged file.",
    )
    parser.add_argument(
        "--lang", default=None, metavar="LANG",
        help="force this language tag for every file's fence.  "
             "Default: derived from each file's extension.",
    )
    parser.add_argument(
        "--no-preamble", dest="include_preamble",
        action="store_false", default=True,
        help="omit the format-explanation preamble and file list.",
    )
    parser.add_argument(
        "--no-language-tag", dest="include_lang_tag",
        action="store_false", default=True,
        help="omit the language tag from the fence opener.",
    )
    parser.add_argument(
        "--group", action="append", nargs="+", default=[],
        metavar="ARGS",
        help="Open a named group. The first ARGS value is the group "
             "name; the rest are files that belong to the group.  "
             "Repeatable.  Grouped and positional modes are mutually "
             "exclusive; inside a group every file still has its own "
             "`MERGED FILE` header, so file and group boundaries are "
             "both unambiguous.",
    )
    args = parser.parse_args()

    # ---- resolve inputs ----
    if args.files and args.group:
        print("error: --group and positional FILE args are mutually "
              "exclusive", file=sys.stderr)
        return 1
    if not args.files and not args.group:
        print("error: no input files (pass FILE args or --group)",
              file=sys.stderr)
        return 1

    file_entries: list[tuple[str | None, Path]] = []
    if args.files:
        for f in args.files:
            file_entries.append((None, f))
    else:
        for grp in args.group:
            if len(grp) < 2:
                print(
                    f"error: --group requires a name and at least "
                    f"one file; got {list(grp)!r}",
                    file=sys.stderr,
                )
                return 1
            group_name = grp[0]
            for f in grp[1:]:
                file_entries.append((group_name, Path(f)))

    # ---- validate inputs ----
    for _name, p in file_entries:
        if not p.is_file():
            print(f"error: not a regular file: {p}", file=sys.stderr)
            return 1

    # ---- resolve output ----
    to_stdout = args.output is not None and str(args.output) == "-"
    out_path: Path | None = None
    if to_stdout:
        fh = sys.stdout
        out_display = "<stdout>"
    else:
        if args.output is None:
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            out_path = Path(f"merged_{stamp}.md")
        else:
            out_path = args.output

        # Refuse to overwrite one of the inputs.
        out_resolved = out_path.resolve()
        for _name, p in file_entries:
            if p.resolve() == out_resolved:
                print(f"error: output path equals input: {p}",
                      file=sys.stderr)
                return 1

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            fh = out_path.open("w", encoding="utf-8")
        except OSError as e:
            print(f"error: cannot write {out_path}: {e}", file=sys.stderr)
            return 1
        out_display = str(out_path)

    total = len(file_entries)
    has_groups = any(n is not None for n, _ in file_entries)

    # Pre-count files per group so the group header can say "N file(s)".
    group_counts: dict[str | None, int] = {}
    for name, _p in file_entries:
        group_counts[name] = group_counts.get(name, 0) + 1

    try:
        if args.title:
            fh.write(f"# {args.title}\n\n")
        if args.include_preamble:
            # Build contiguous runs of (name, files) for the tree
            # renderer.  In grouped mode every run corresponds to one
            # `--group`; in flat mode there's a single run with
            # `name=None` (the renderer labels it `(flat)`), but we
            # take the numbered-list branch instead.
            ordered_groups: list[tuple[str | None, list[Path]]] = []
            cur_name: str | None = None
            cur_files: list[Path] = []
            for name, path in file_entries:
                if name != cur_name:
                    if cur_files:
                        ordered_groups.append((cur_name, cur_files))
                    cur_name, cur_files = name, [path]
                else:
                    cur_files.append(path)
            if cur_files:
                ordered_groups.append((cur_name, cur_files))

            root_label = (out_path.name if out_path is not None
                          else "merged")
            fh.write(_preamble(
                total,
                [p for _, p in file_entries],
                groups=ordered_groups,
                has_groups=has_groups,
                root_label=root_label,
            ))
            fh.write("\n")

        current_group: str | None = None
        for i, (name, path) in enumerate(file_entries, 1):
            if name != current_group:
                if name is not None:
                    fh.write(_group_header(name,
                                           group_counts[name]))
                current_group = name
            content = _read_file(path)
            language = _language_for(path, args.lang)
            _emit_block(fh, i, total, path, content, language,
                        emit_lang_tag=args.include_lang_tag)
    finally:
        if not to_stdout:
            fh.close()

    if not to_stdout:
        print(f"Wrote {out_display} ({total} file(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
