"""recover.preexisting — pre-existing baseline loader.

STABLE.  Walk `--apply-to` and synthesise one `Block` per source file
with `classified_by="preexisting"` and `kind="full"`.  These baselines
are prepended to the conversation's blocks and act as the base content
for matching records.

The exclusion lists (which directories, extensions, and file sizes are
ignored) encode domain knowledge about what counts as "source" in a
project tree — they change rarely.

See `pipeline.py`'s module docstring, PRE-EXISTING BASELINES, for the
historical context on why untouched pre-existing files flow through
passes 5-9 normally and are skipped by the writer.
"""

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


def load_preexisting_blocks(apply_to: Path) -> list[Block]:
    """Scan `apply_to` and return one synthetic 'full print' block per
    source-like file, ready to be prepended to the conversation's
    blocks.

    Filters out:
      * dot-directories and well-known build/cache directories
      * extensions not in `SOURCE_EXTENSIONS`
      * extensions in `_PREEXISTING_EXCLUDE_EXTENSIONS` (docs, data,
        config — never imported by source code)
      * files larger than `_PREEXISTING_MAX_FILE_SIZE`
    """
    blocks: list[Block] = []
    if not apply_to.is_dir():
        return blocks

    for p in sorted(apply_to.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(apply_to)
        rel_parts = rel.parts

        if any(part.startswith(".") for part in rel_parts):
            continue
        if any(part in _PREEXISTING_EXCLUDE_DIRS for part in rel_parts):
            continue
        if p.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        if p.suffix.lower() in _PREEXISTING_EXCLUDE_EXTENSIONS:
            continue
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

        rel_str = rel.as_posix()
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
    return blocks


def existing_dirs_for(apply_to: Optional[Path]) -> set:
    """Return the set of relative directory paths under `apply_to`.

    Used by `topic.infer_topic_weights` to filter its output to
    directories that actually exist in the target tree.  Returns
    the empty set when `apply_to` is None or missing.
    """
    if apply_to is None or not apply_to.is_dir():
        return set()
    out: set = set()
    for p in apply_to.rglob("*"):
        if not p.is_dir():
            continue
        try:
            rel = p.relative_to(apply_to)
        except ValueError:
            continue
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            continue
        if any(part in _PREEXISTING_EXCLUDE_DIRS for part in parts):
            continue
        out.add("/".join(parts))
    return out