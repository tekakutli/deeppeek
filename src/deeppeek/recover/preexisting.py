"""recover.preexisting — pre-existing baseline loader.

STABLE.  Walk `--apply-to` and synthesise one `Block` per source file
with `classified_by="preexisting"` and `kind="full"`.  These baselines
are prepended to the conversation's blocks and act as the base content
for matching records.

SINGLE-WALK (2026-11-05)
========================
Prior revisions walked the target tree three times per run:

  · `load_preexisting_blocks` — load every source file as a baseline
  · `existing_dirs_for` — collect directories for topic inference
  · `pass9_write`'s `on_disk` — build a name set for MISSING_FILES

Each walk used `rglob("*")` with post-filter exclusion, so
`node_modules` and friends were traversed and then discarded.  The new
`scan_apply_to` collapses all three into one `os.walk` whose
`dirnames[:]` assignment prunes excluded directories *before* the walk
descends.  `node_modules` is now never visited.

The pipeline calls `scan_apply_to` once (after pass 1) and threads all
three products forward: baselines to the block list, directories to
`infer_topic_weights`, on-disk names to `pass9_write`.

The legacy entry points (`load_preexisting_blocks`, `existing_dirs_for`)
remain as thin wrappers for out-of-tree callers.  They re-scan, but the
pipeline no longer calls them.
"""

import os
from pathlib import Path
from typing import Optional

from .core import Block, SOURCE_EXTENSIONS


_PREEXISTING_EXCLUDE_DIRS = {
    "node_modules", "__pycache__", "dist", "build", "target",
    "venv", ".venv", ".tox", ".nox", ".git",
    "deeppeek",
    ".deeppeek",
}
_PREEXISTING_MAX_FILE_SIZE = 1_048_576  # 1 MiB

# Data/config extensions.  None of these are ever imported by a
# source file, so loading them as pre-existing baselines gives
# passes 5-7 records that need no treatment and bloat warnings.txt
# with `[MISSING] no full print` lines for panel history files.
_PREEXISTING_EXCLUDE_EXTENSIONS = frozenset({
    ".md", ".rst", ".txt",
    ".json", ".yaml", ".yml", ".toml",
})


def scan_apply_to(apply_to: Path) -> tuple[list[Block], set, set]:
    """Single-pass scan of `apply_to`.

    Returns `(blocks, existing_dirs, on_disk_names)`:

      · `blocks` — one synthetic full-print Block per source file,
        ready to be prepended to the conversation's block list.

      · `existing_dirs` — relative directory paths under `apply_to`
        (e.g. `{"playgrounds/inpaint", "playgrounds/board"}`), used
        by `topic.infer_topic_weights` to filter its output.

      · `on_disk_names` — basenames of every file the walk visited
        (any extension, including excluded dirs are NOT in this set
        because they're pruned).  Consumed by `pass9_write` for the
        MISSING_FILES scan so it doesn't walk the tree again.

    Directory exclusion uses `dirnames[:] = [...]` in-place, which
    `os.walk` respects for all subsequent recursion — the excluded
    subtree is never visited.  The previous `rglob("*")` version
    descended into `node_modules` and filtered afterwards, paying the
    I/O for files that were then discarded.
    """
    blocks: list[Block] = []
    existing_dirs: set = set()
    on_disk_names: set = set()

    if not apply_to.is_dir():
        return blocks, existing_dirs, on_disk_names

    for dirpath, dirnames, filenames in os.walk(apply_to):
        # Prune in place — os.walk will not recurse into the removed
        # entries.  This is the whole performance win: excluded dirs
        # are never entered, not traversed-and-discarded.
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and d not in _PREEXISTING_EXCLUDE_DIRS
        ]

        base = Path(dirpath)
        try:
            rel_base = base.relative_to(apply_to)
        except ValueError:
            continue

        if rel_base.parts:
            existing_dirs.add("/".join(rel_base.parts))

        for fn in filenames:
            on_disk_names.add(fn)
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SOURCE_EXTENSIONS:
                continue
            if ext in _PREEXISTING_EXCLUDE_EXTENSIONS:
                continue
            p = base / fn
            try:
                if p.stat().st_size > _PREEXISTING_MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            try:
                raw = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            body = raw.replace("\r\n", "\n")
            if body.endswith("\n"):
                body = body[:-1]

            rel_str = (rel_base / fn).as_posix()
            blocks.append(Block(
                idx=0,
                header_path=rel_str,
                modifier=None,
                fence_lang="",
                body=body,
                body_line_count=body.count("\n") + 1,
                context_before="",
                line_start=-1,
                line_end=-1,
                is_orphan=False,
                is_code_file=True,
                kind="full",
                canonical_path=rel_str,
                output_path=rel_str,
                confidence=1.0,
                classified_by="preexisting",
                heuristic_kind="full",
            ))

    return blocks, existing_dirs, on_disk_names


def load_preexisting_blocks(apply_to: Path) -> list[Block]:
    """Backward-compat wrapper.  New code should call `scan_apply_to`
    and use the returned `(blocks, dirs, names)` triple, avoiding a
    second tree walk."""
    blocks, _, _ = scan_apply_to(apply_to)
    return blocks


def existing_dirs_for(apply_to: Optional[Path]) -> set:
    """Backward-compat wrapper.  New code should call `scan_apply_to`."""
    if apply_to is None:
        return set()
    _, dirs, _ = scan_apply_to(apply_to)
    return dirs
