"""recover.shield — Pass 8.8: fail-closed shield for target-tree
                     overwrites.

A last-resort safety net for `--apply-to` mode. Even after the dedup
guard (`pass5`) and the partial-reprint demotion (`pass6`), and the
cross-validation demotions (`pass7.6b`, `pass7.8`), a bug in any of
those layers could still let a record through whose content is
provably incomplete. This pass compares the recovered content
against what is *actually on disk* in the target tree, and refuses
to write a file that would delete substantive code from it.

The motivating failure::

    `comfyui_krita.py` was recovered as 31 lines (2 top-level
    functions) but the target tree's version is 437 lines (22
    top-level functions). The conversation block that "replaced"
    the file was a partial reprint of two functions with a couple
    of helper calls that happened to mention identifiers the
    pre-existing file didn't have — which defeated both the
    verbatim-substring check and the token-subset check in
    `_token_subset`, so the dedup guard picked the truncated block
    as the base. Twenty of twenty-two top-level declarations were
    silently deleted.

    With the shield in place, `pass8_8` reads the target's version
    from disk, extracts its top-level declarations, and — seeing
    that twenty would disappear — refuses the write. The record is
    demoted to LOW and the target file survives intact.

Bounds and false positives
==========================
The shield fires only when:

  * `apply_to` is set (target-tree mode; not `--out` mode)
  * the record is not a pre-existing base (those are unchanged
    on disk by construction)
  * the target file exists on disk and is readable
  * the recovered content removes at least
    `max(2, len(old_decls) // 4)` top-level declarations from
    what's on disk

The `max(2, ...)` floor means:

  * a 2-declaration file losing 1 declaration does not fire (the
    threshold is 2)
  * a 20-declaration file losing 1 declaration does not fire (the
    threshold is 5)
  * a 20-declaration file losing 6 fires (6 >= 5)

This handles the common cases:

  · "The AI refactored one function into three" — net declarations
    went up, shield silent.
  · "The AI renamed one function" — 1 gone, shield silent.
  · "The AI rewrote the whole file, dropping half the helpers" —
    half gone, shield fires.

The threshold is deliberately low enough to catch the catastrophic
case, but high enough to not fire on routine renames and
single-function deletions.

`--min-trust LOW` is the escape hatch for users who genuinely want
the removal: they already told the pipeline "write everything,"
and the shield respects that by demoting to LOW (which is written
under min_trust LOW) rather than blocking outright. The audit
trail — `verification.shield` in the manifest — tells them what
they overrode.

Language coverage
=================
The shield uses `_top_level_decls` from `dedup.py`, which matches
`def`/`class` for Python and `export`/`function`/`class`/`const`/
`let`/`var` for JS. Files in other languages (HTML, JSON, Markdown)
are unaffected: `_top_level_decls` returns an empty set for them,
and both sides of the diff are empty, so the shield is silent.

Non-interaction with other passes
=================================
The shield runs after `pass8_7_reconcile`. A record that reconcile
just promoted will have `rec.trust == "MEDIUM"` or `"HIGH"` and
`rec.verification["reconciled"]` set; those are exactly the records
the shield should scrutinise, because reconcile's promotions are
"fixed up after excluding patches" — a state that can still ship
incomplete content. The shield does not care how a record reached
its current trust; it only checks the target tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .core import FileRecord
from .dedup import _top_level_decls


def pass8_8_shield_preexisting(records: dict,
                               apply_to: Path,
                               verbose: bool = False) -> int:
    """Fail closed on overwrites that would delete substantive
    declarations from the target tree.

    Returns the number of records demoted. Mutates `records` in place.
    """
    if apply_to is None:
        return 0

    apply_to = Path(apply_to)
    n_demoted = 0

    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.preexisting_base:
            # Pre-existing base: the target file is the source. There's
            # nothing to shield against — the write either preserves
            # it or is skipped by the preexisting_unmodified branch
            # in pass9.
            continue
        if rec.trust == "LOW":
            # Already failing closed; the shield doesn't need to
            # re-demote.
            continue

        target_path = apply_to / path
        if not target_path.is_file():
            # New file: no pre-existing content to remove.
            continue

        try:
            old_content = target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable target: not our problem to shield. The
            # patcher's own write will fail on its own.
            continue

        old_decls = _top_level_decls(old_content)
        new_decls = _top_level_decls(rec.content)
        missing = old_decls - new_decls

        threshold = max(2, len(old_decls) // 4)
        if not missing or len(missing) < threshold:
            continue

        rec.trust = "LOW"
        rec.verification["shield"] = {
            "target": str(target_path),
            "missing_count": len(missing),
            "missing": sorted(missing)[:20],
            "old_decl_count": len(old_decls),
            "new_decl_count": len(new_decls),
            "threshold": threshold,
        }
        rec.warnings.append(
            f"shield: recovered content would remove "
            f"{len(missing)} of {len(old_decls)} top-level "
            f"declaration(s) present in the target file "
            f"({target_path.name}): "
            f"{sorted(missing)[:6]}"
            f"{'...' if len(missing) > 6 else ''}"
            f" — failing closed to protect the target tree"
        )
        n_demoted += 1

        if verbose:
            print(f"  [shield] {path}: would remove "
                  f"{len(missing)}/{len(old_decls)} declarations; "
                  f"fail-closed", file=sys.stderr)

    return n_demoted
