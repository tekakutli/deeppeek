"""Persist the most recent conversation dump to a self-gitignored dir.

`pipeline.run()` calls `save_last(dump_text)` after resolving the
input dump (only when the input came from a file — a `capture`
without `-o` keeps its dump in memory and has nothing on disk yet
to save).  The saved file is `.deeppeek/last.md` in the directory
`deeppeek recover` was run from.

Only one file is ever kept: each run overwrites the previous one.
The `.deeppeek/` directory is created with its own `.gitignore`
containing `*`, so the saved file is never committed regardless of
whether the enclosing repo's `.gitignore` mentions it.

`deeppeek restore` copies the saved file to a user-specified path
— see `commands/restore.py`.
"""
from pathlib import Path


LAST_DIR_NAME = ".deeppeek"
LAST_FILE_NAME = "last.md"
GITIGNORE_CONTENT = "*\n"


def last_dir(base: Path = None) -> Path:
    """Return the directory that holds the saved conversation."""
    base = Path.cwd() if base is None else base
    return base / LAST_DIR_NAME


def last_path(base: Path = None) -> Path:
    """Return the full path to the saved conversation file."""
    return last_dir(base) / LAST_FILE_NAME


def save_last(text: str, base: Path = None) -> Path:
    """Write `text` to `<cwd>/.deeppeek/last.md`; return the path.

    Creates the directory and its self-gitignore on first call.
    Overwrites any existing file.  Idempotent: writing the same text
    twice produces byte-identical content and a no-op diff.
    """
    d = last_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.is_file():
        gi.write_text(GITIGNORE_CONTENT, encoding="utf-8")
    p = d / LAST_FILE_NAME
    p.write_text(text, encoding="utf-8")
    return p


def get_last(base: Path = None) -> Path:
    """Return the path to the saved conversation; raise if absent."""
    p = last_path(base)
    if not p.is_file():
        raise FileNotFoundError(
            f"no saved conversation at {p}; run "
            f"`deeppeek recover <conversation.md>` first")
    return p
