#!/usr/bin/env python3
"""
dumps_casted.py — overlay one dump onto another, anchored to --old.

Given two dumps produced by dump.py:

  --old PATH    the anchor dump; the output has exactly this file set
  --new PATH    the source of newer content

produce a new dump where the file set is anchored to --old, but each
old file whose path also appears in --new with DIFFERENT content is
replaced by the --new version.

Decision table (per old file):

  present in new, same content       → kept (no-op)
  present in new, different content  → new content wins
  absent in new                      → kept verbatim from old

Files that exist only in --new are ignored — the point of the cast is
to refresh a known file set, not to grow it.  This mirrors the
"--only for those files that exist (as dumps) on --old" contract:
new files never enter the output, and old files never leave.

Usage:
    dumps_casted.py --old OLD.md --new NEW.md
    dumps_casted.py --old OLD.md --new NEW.md -o CAST.md
    dumps_casted.py --old OLD.md --new NEW.md -o -       # stdout
    dumps_casted.py --old OLD.md --new NEW.md --strip-tree

Default output: <old_stem>_cast<suffix> next to --old.

Parser and DIRECTORY TREE handling come from dumps_diff.py (loaded by
path, same pattern dumps_evolve.py uses), so the output composes
cleanly with the rest of the dump tooling.

The DIRECTORY TREE section, if present in --old, is preserved by
default: since the file set is unchanged from --old, old's tree
remains accurate even after content replacement.  Pass --strip-tree
to drop it.
"""

import argparse
import importlib.util
import io
import sys
from pathlib import Path

SCRIPT_DIR  = Path(__file__).resolve().parent
DIFF_SCRIPT = SCRIPT_DIR / "dumps_diff.py"

HEADER = "=" * 70

# Triple-backtick, built at runtime so this file's own markdown
# fences are never broken by its source.
_BT3 = "`" * 3

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
    return _load("casted_diff", DIFF_SCRIPT)


def cast(old_files, new_map):
    """Return [(path, content)] preserving --old's file order.

    Per-file rule: use --new's content when the path exists in --new
    with different content; otherwise keep --old's content verbatim.

    `new_map` is a dict {path: content} built from --new, so lookups
    are O(1) and duplicate paths in --new are collapsed to the last
    occurrence (matching how dump.py's own parser produces a unique
    path list per dump).
    """
    result = []
    for path, old_content in old_files:
        if path in new_map and new_map[path] != old_content:
            result.append((path, new_map[path]))
        else:
            result.append((path, old_content))
    return result


def _write_body(fh, title, preamble, files, keep_tree):
    """Write the full dump — header, preamble (optionally stripped of
    its DIRECTORY TREE), then FILE i/N blocks — to `fh`."""
    diff = load_diff()
    if not keep_tree:
        preamble = diff.strip_tree(preamble)
    total = len(files)
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


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dumps_casted.py",
        description=(
            "Overlay one dump onto another, anchored to --old: the "
            "output has exactly --old's file set, but each file whose "
            "path also appears in --new with different content is "
            "updated to --new's version.  Files that exist only in "
            "--new are ignored."
        ),
    )
    ap.add_argument(
        "--old", type=Path, required=True, metavar="PATH",
        help="anchor dump; the output contains exactly this file set",
    )
    ap.add_argument(
        "--new", type=Path, required=True, metavar="PATH",
        help="content source; when a matching path exists here with "
             "different content, --new's version replaces --old's",
    )
    ap.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="output path.  \"-\" writes the dump to stdout.  "
             "Default: <old_stem>_cast<suffix> next to --old.",
    )
    ap.add_argument(
        "--strip-tree", action="store_true",
        help="Drop the DIRECTORY TREE section from the output.  By "
             "default it is preserved (it remains accurate, since "
             "the file set is unchanged from --old).",
    )
    args = ap.parse_args(argv)

    if not args.old.is_file():
        print(f"error: --old is not a file: {args.old}", file=sys.stderr)
        return 1
    if not args.new.is_file():
        print(f"error: --new is not a file: {args.new}", file=sys.stderr)
        return 1

    diff = load_diff()

    try:
        old_text = args.old.read_text(encoding="utf-8")
        new_text = args.new.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read input: {e}", file=sys.stderr)
        return 1

    old_preamble, old_files = diff.parse_dump(old_text)
    _, new_files = diff.parse_dump(new_text)

    if not old_files:
        print(f"error: no FILE blocks parsed from {args.old}",
              file=sys.stderr)
        return 1
    if not new_files:
        print(f"warning: no FILE blocks parsed from {args.new}; "
              f"output will be identical to {args.old}",
              file=sys.stderr)

    new_map = dict(new_files)
    old_map = dict(old_files)

    casted = cast(old_files, new_map)

    replaced = sum(
        1 for path, old_content in old_files
        if path in new_map and new_map[path] != old_content
    )
    kept = len(old_files) - replaced
    new_only = sum(1 for path in new_map if path not in old_map)

    out_path = args.output
    if out_path is None:
        out_path = args.old.with_name(
            f"{args.old.stem}_cast{args.old.suffix}"
        )
    to_stdout = str(out_path) == "-"

    title = (f"CAST of {args.old.name} with content from "
             f"{args.new.name}")

    buf = io.StringIO()
    _write_body(buf, title, old_preamble, casted,
                keep_tree=not args.strip_tree)
    body = buf.getvalue()
    line_count = len(body.splitlines())

    if to_stdout:
        sys.stdout.write(body)
        sys.stdout.flush()
        target = "<stdout>"
    else:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(body, encoding="utf-8")
        except OSError as e:
            print(f"error: cannot write {out_path}: {e}",
                  file=sys.stderr)
            return 1
        target = str(out_path)

    print(f"cast {args.old.name} <- {args.new.name}", file=sys.stderr)
    print(f"  files in old:      {len(old_files)}", file=sys.stderr)
    print(f"  replaced from new: {replaced}", file=sys.stderr)
    print(f"  kept from old:     {kept}", file=sys.stderr)
    if new_only:
        print(f"  new-only ignored:  {new_only}", file=sys.stderr)
    print(f"  total lines:       {line_count}", file=sys.stderr)
    print(f"Wrote {len(casted)} file(s) to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
