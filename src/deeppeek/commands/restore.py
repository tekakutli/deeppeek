"""`deeppeek restore` — copy the last-recovered conversation to a path.

Whenever `deeppeek recover <file>` runs against a file (rather than
an in-memory capture), the file's content is saved to
`<cwd>/.deeppeek/last.md`.  This command copies that saved file to a
user-specified destination.  See `recover/last_conversation.py` for
the save side.

Usage:
    deeppeek restore                    # → ./last_conversation.md
    deeppeek restore /tmp/copy.md       # → /tmp/copy.md
    deeppeek restore /tmp/              # → /tmp/last.md

The saved file is always a verbatim copy of the original
`conversation.md` the user passed to `recover` — never a
reconstruction, never the residual.
"""
import argparse
import shutil
import sys
from pathlib import Path

from ..recover.last_conversation import get_last, LAST_FILE_NAME


DEFAULT_DEST = Path("last_conversation.md")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "restore",
        help="Copy the most recently recovered conversation file to a "
             "specified path",
    )
    p.add_argument(
        "destination", nargs="?", default=None, type=Path,
        metavar="DEST",
        help=f"where to copy the saved conversation.  If DEST is an "
             f"existing directory, the file lands inside it as "
             f"{LAST_FILE_NAME!r}; otherwise DEST is used as the full "
             f"target filename.  Default: ./{DEFAULT_DEST}",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        src = get_last()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.destination is None:
        dest = DEFAULT_DEST
    elif args.destination.is_dir():
        dest = args.destination / LAST_FILE_NAME
    else:
        dest = args.destination

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except OSError as e:
        print(f"error: cannot copy {src} to {dest}: {e}",
              file=sys.stderr)
        return 1

    print(f"Copied {src} → {dest}", file=sys.stderr)
    return 0
