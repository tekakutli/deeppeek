"""`deeppeek residual` — list the files a residual.md still references.

Residual.md is generated after a `recover --apply-here` run (see
`recover/residual.py`).  It contains the blocks that did not reach
a successful placement against the target tree.  This command parses
it and lists the files those blocks touch, so the user can see at
a glance what still needs attention without opening the file.

Output is two lists:

  · All file references — one entry per residual block, in block
    order, with its outcome tag.  Repeated files appear once per
    block that mentions them; blocks without a `## File:` header
    (unattributed orphans) appear as `<no file>`.

  · Unique files — the deduplicated set, sorted alphabetically,
    with a per-file outcome breakdown (how many blocks of each
    outcome reference that file).

Default path: ./deeppeek/residual.md — the location a
`deeppeek recover <conversation.md> --apply-here` run writes to
when run from the target directory.
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from ..recover.residual import parse_residual_entries


def run(args: argparse.Namespace) -> int:
    path = (args.path if args.path is not None
            else Path("deeppeek/residual.md"))
    if not path.is_file():
        print(f"error: no residual file at {path}", file=sys.stderr)
        print("  (default: ./deeppeek/residual.md — write this "
              "with `deeppeek recover <conversation.md> --apply-here`)",
              file=sys.stderr)
        return 1
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 1

    entries = parse_residual_entries(text)
    n_blocks = len(entries)

    by_file = defaultdict(Counter)
    for p, outcome in entries:
        if p is not None:
            by_file[p][outcome or "?"] += 1

    n_with_file = sum(sum(c.values()) for c in by_file.values())
    n_without_file = n_blocks - n_with_file

    print(f"residual.md: {path}")
    print(f"  blocks:        {n_blocks}")
    print(f"  with file:     {n_with_file}")
    print(f"  no file:       {n_without_file} (unattributed)")
    print(f"  unique files:  {len(by_file)}")
    print()

    if n_blocks == 0:
        print("(residual contains no blocks)")
        return 0

    print(f"All file references ({n_blocks} blocks, in order):")
    width = len(str(n_blocks - 1))
    for i, (p, outcome) in enumerate(entries):
        label = p if p is not None else "<no file>"
        suffix = f"  [{outcome}]" if outcome else ""
        print(f"  [{i:>{width}}] {label}{suffix}")
    print()

    if by_file:
        print(f"Unique files ({len(by_file)}):")
        for p in sorted(by_file):
            breakdown = ", ".join(
                f"{n} {outcome}"
                for outcome, n in sorted(by_file[p].items()))
            print(f"  {p}  ({breakdown})")
    else:
        print("Unique files: (none — no file headers found)")

    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "residual",
        help="List the files a residual.md still references",
    )
    p.add_argument(
        "path", nargs="?", type=Path, default=None, metavar="PATH",
        help="path to residual.md (default: ./deeppeek/residual.md)",
    )
    p.set_defaults(func=run)
