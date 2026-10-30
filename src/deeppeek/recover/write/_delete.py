"""Filesystem deletion for pass 9's `--apply-to` mode.

STABLE.  Applies the `[DEL]` / `[DELETED]` / `[REMOVED]` markers
extracted from the conversation against the target tree.  The
resolver here is a small filesystem walk: exact path, then basename
at the root, then unique basename anywhere under `apply_to` (with
`.deeppeek`/`deeppeek` excluded).  If two files share the basename,
the deletion is refused and recorded as `[AMBIGUOUS]`.

Nothing in this file changes when pass 9 grows.  New deletion-marker
syntaxes (`rm -rf`, `DELETE:`, ...) get added to `core.DEL_MARKER_RE`,
not here.
"""

import os
from pathlib import Path


def _apply_deletions(apply_to: Path,
                     all_deleted: list,
                     warnings_lines: list) -> tuple:
    """Remove every file in `all_deleted` from `apply_to`.

    Appends one line to `warnings_lines` per outcome.  Returns
    `(applied, missing)` — the two name lists suitable for
    `deleted.txt`.
    """
    applied: list = []
    missing: list = []
    for name in all_deleted:
        base = os.path.basename(name)
        candidates = []
        if base and base != name:
            candidates.append(apply_to / base)
        candidates.append(apply_to / name)

        target = None
        for c in candidates:
            if c.exists() and c.is_file():
                target = c
                break

        if target is None and base:
            matches = [
                p for p in apply_to.rglob(base)
                if p.is_file() and p.name == base
                and "deeppeek" not in p.parts
                and ".deeppeek" not in p.parts
            ]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                rel_matches = [
                    m.relative_to(apply_to).as_posix() for m in matches
                ]
                warnings_lines.append(
                    f"[{name}] [AMBIGUOUS] {len(matches)} files match "
                    f"basename ({rel_matches}); not deleted")
                missing.append(name)
                continue

        if target is None:
            missing.append(name)
            continue

        try:
            target.unlink()
            applied.append(name)
            warnings_lines.append(
                f"[{name}] [DELETED] removed from target tree")
        except Exception as e:
            warnings_lines.append(
                f"[{name}] [ERROR] could not delete: {e}")
            missing.append(name)
    return applied, missing
