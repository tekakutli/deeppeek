#!/usr/bin/env python3
"""merge_md.py — merge input files into a single markdown file.

Usage:
    merge_md.py [-o OUT] FILE [FILE ...]

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


def _preamble(total: int, files: list[Path]) -> str:
    lines = [
        "# Merged file",
        "",
        f"{total} file(s) merged:",
        "",
    ]
    for i, p in enumerate(files, 1):
        lines.append(f"{i}. {p}")
    lines += [
        "",
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
        "files", nargs="+", type=Path, metavar="FILE",
        help="files to merge, in input order",
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
    args = parser.parse_args()

    # ---- validate inputs ----
    for p in args.files:
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
        for p in args.files:
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

    total = len(args.files)
    try:
        if args.title:
            fh.write(f"# {args.title}\n\n")
        if args.include_preamble:
            fh.write(_preamble(total, args.files))
            fh.write("\n")

        for i, path in enumerate(args.files, 1):
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
