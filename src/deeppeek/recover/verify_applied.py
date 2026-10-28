"""
verify_applied — Pass 8.7c: verify that patches marked
                 `deterministic` (or `llm`) have their added lines
                 still present in the final content.  Warn-only.

Motivating case (2026-10-28 comfyui.py): patches 97 and 99 were both
marked `applied_by: "deterministic"`, and the final written file
lacks their contributions entirely.  A prior version of this pass
demoted the record to LOW, which routed the whole file into
`deeppeek/low_trust/` and left `--output_rotation` silently
accepted-and-discarded on the target.

THE GOVERNING PHILOSOPHY
========================
The tool's job is to place what the patches say.  If the conversation
tells us to add a parameter and never wire it up, the tool writes the
parameter-declared, body-ignores-it file.  That the resulting feature
is dead is the conversation's problem, not the tool's.  This pass's
job is to *surface* that kind of gap — to say "patch 62's lines are
gone; patch 99's lines are gone" — not to gate the write on the
conversation having been complete.

An earlier version demoted; it produced three classes of false
positive that we now have concrete evidence for:

  · Supersession.  Patch 62 adds an arg-entry to `args={}`.  Patch 95
    later replaces the whole `args={}` dict with a version whose help
    text differs in one character.  Patch 62's exact bytes are gone —
    because 95 superseded them.  The final content is what both
    patches together intended; the record should ship.

  · Transform-shaped patches.  `_try_object_literal_splice` merges
    `board = {…}` into `const board = {…}`; the body's prefix line is
    legitimately absent.  `_try_ellipsis_splice` consumes the `...`
    marker by design.  The bytes are absent because the transform
    wanted them absent.

  · Incomplete features.  `_paste_result` gained
    `output_rotation=...` and never uses it.  That is what the
    patches said; that is what the file should contain.  The
    `added-param-unused` pass already reports this as a warning; the
    verify-applied pass does not need to *also* demote on the same
    fact.

WHAT IT DETECTS
===============
For each patch marked `applied_by` in {`deterministic`, `llm`}:

  1. Extract the patch's *added* lines — those in the body but not
     in the base.
  2. For each added line, check presence in the final content.
  3. If any are missing, classify the patch's shape (insert vs
     transform) and warn accordingly.

Added-line extraction differs for normal vs. diff patches:

  · Normal patches: every non-blank body line whose stripped form is
    not already present in the base.
  · Diff patches: only the `+`-prefixed lines.

WARN CLASSIFICATION
===================
Insert-shaped patches — the patcher's job was to place bytes.  A
missing added line is a real signal of "the patch may have been
reverted".  Warning text: "verify-applied: patch idx=N … missing
content lines".  Informational only.

Transform-shaped patches — the patcher rewrote existing content into
the body's shape (scope-decl body whose scope exists in base;
ellipsis marker; diff body; signature-only fragment).  Partial
missing lines are normal.  Warn *only when every added line is
missing*, which signals a wholesale overwrite: "advisory: patch
idx=N … all added lines missing".

The shape classifier is the load-bearing piece: without it, every
object-literal splice and every ellipsis splice would warn on the
transform's normal byte-diff.  See `_is_insert_shaped`.

NO DEMOTION.  Neither warning changes `rec.trust`.  The manifest
populates `verification.patch_content_missing` **only for warned
cases** — the field mirrors `warnings.txt` for machine consumers.
Partial transform misses (byte-diffs from legitimate splices) are
not recorded.  The file that ships is exactly the file that would
ship if this pass did not exist.

ORDERING
========
Runs after pass 8.7b (reverify) and before pass 8.8 (shield).  Reads
`blocks_by_idx[rec.last_full_idx].body` as the base — consistent
with `pass6_apply_patches` and every other base-reading pass.

PAIRS ARE NOT CHECKED
=====================
This pass only inspects `rec.patches`.  Replace pairs are structural:
`new_body` replaces `old_body` wholesale, so if a pair is marked
`deterministic` its new body is present by construction.  Distinguishing
"pair's new body overwritten by a later patch" from "pair's new body
partially updated by a later patch, which is fine" requires semantic
understanding out of scope for a warning pass.

KNOWN GAPS
==========
  · Line-presence is stripped-exact.  A patch that adds `foo(a, b)`
    and a later pass reformats to `foo(a,b)` reports "missing" — but
    that is the point: the pass surfaces the discrepancy for human
    inspection.  No demotion means a false positive costs a warning,
    not a skipped file.

  · `_is_insert_shaped` uses `_scope_exists_any_kind` to classify
    scope-decl bodies.  A scope name that appears only inside a base
    string literal would be misclassified as transform (missing
    warning); a false negative at the warning level, acceptable.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

from .core import FileRecord, _looks_like_diff_body
from .patch_scope import _scope_exists_any_kind
from .patch_text import _has_ellipsis_marker
from .prose import _extract_scope_from_body


_VERIFIED_STATUSES = frozenset({"deterministic", "llm"})


_SIGNATURE_HEAD_RE = re.compile(
    r'^(?:export\s+)?(?:async\s+)?(?:def|function|class)\s+'
)


def _base_for(rec: FileRecord,
              blocks_by_idx: Optional[dict]) -> Optional[str]:
    """Return the pre-application base content for `rec`, or None."""
    if blocks_by_idx is None or rec.last_full_idx is None:
        return None
    block = blocks_by_idx.get(rec.last_full_idx)
    if block is None:
        return None
    return block.body


def _patch_added_lines(patch_body: str, base_lines: set) -> list:
    """Return the stripped lines that `patch_body` contributes and
    that are not already present in `base_lines`."""
    if _looks_like_diff_body(patch_body):
        out = []
        for pl in patch_body.splitlines():
            if not pl.startswith("+"):
                continue
            if pl.startswith("+++"):
                continue
            content = pl[1:].strip()
            if content and content not in base_lines:
                out.append(content)
        return out
    out = []
    for pl in patch_body.splitlines():
        content = pl.strip()
        if content and content not in base_lines:
            out.append(content)
    return out


def _is_insert_shaped(patch_body: str, base: str) -> bool:
    """True iff the patcher's job was to PLACE these bytes, not to
    TRANSFORM existing content into them.

    Returns False for transform shapes:
      · Ellipsis marker — `_try_ellipsis_splice` consumes the marker.
      · Diff body — `_try_raw_diff_patch` applies `+`/`-` selectively.
      · Signature-only body — first non-blank line (lstripped) matches
        `_SIGNATURE_HEAD_RE` AND last non-blank line ends with `:`.
        Both conditions required: a full function body (`def foo():\\n
        return 1`) has a last line ending with a statement, not a
        colon, so it is NOT signature-only and IS insert-shaped.
      · Scope-declaration body whose scope exists in base.

    Returns True for everything else, including: bare `if (…) {…}`
    guards, `foo(…, kw=v)` calls, `try:/except:` blocks, new function
    declarations whose names are not in base.
    """
    if not patch_body or not patch_body.strip():
        return False

    if _has_ellipsis_marker(patch_body):
        return False

    if _looks_like_diff_body(patch_body):
        return False

    try:
        scope_name, _scope_kind = _extract_scope_from_body(
            patch_body.strip())
    except Exception:
        scope_name = None
    if scope_name:
        try:
            if _scope_exists_any_kind(base, scope_name):
                return False
        except Exception:
            return False

    non_blank = [ln.rstrip() for ln in patch_body.splitlines()
                 if ln.strip()]
    if non_blank:
        first_line = non_blank[0].lstrip()
        last_line = non_blank[-1]
        if (_SIGNATURE_HEAD_RE.match(first_line)
                and last_line.endswith(":")):
            return False

    return True


def _check_patch_landed(patch_body: str, base: str, final: str) -> list:
    """Return the stripped lines the patch added that are not in
    the final content."""
    base_lines = {ln.strip() for ln in base.splitlines() if ln.strip()}
    final_lines = {ln.strip() for ln in final.splitlines() if ln.strip()}
    added = _patch_added_lines(patch_body, base_lines)
    return [ln for ln in added if ln not in final_lines]


def pass8_7c_verify_applied_patches(
        records: dict[str, FileRecord],
        blocks_by_idx: Optional[dict] = None) -> int:
    """Report (never demote) patches whose added lines are not in the
    final content.

    Returns the number of warnings emitted.  Trust is not modified for
    any record.

    `verification.patch_content_missing` is populated **only for the
    cases we warn on**: insert-shaped patches with any missing line,
    and transform-shaped patches whose every added line is missing.
    Partial transform misses (normal splice byte-diffs) are not
    recorded — they are correct behaviour, not discrepancies.  The
    field mirrors `warnings.txt` for machine consumers.
    """
    n_warned = 0
    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.is_deleted:
            continue
        if not rec.patches:
            continue

        base = _base_for(rec, blocks_by_idx)
        if base is None:
            continue

        base_lines = {ln.strip() for ln in base.splitlines()
                      if ln.strip()}
        warned: list = []

        for patch in rec.patches:
            status = patch.get("applied_by")
            if status not in _VERIFIED_STATUSES:
                continue
            body = patch.get("body") or ""
            if not body.strip():
                continue
            idx = patch.get("idx")

            try:
                missing = _check_patch_landed(body, base, rec.content)
            except Exception as e:
                rec.warnings.append(
                    f"verify-applied: patch idx={idx} check raised: {e}"
                )
                continue
            if not missing:
                continue

            try:
                added = _patch_added_lines(body, base_lines)
            except Exception:
                added = missing

            insert_shaped = _is_insert_shaped(body, base)

            entry = {
                "idx": idx,
                "missing_count": len(missing),
                "added_count": len(added),
                "first_missing": missing[0][:80],
                "status": status,
                "insert_shaped": insert_shaped,
            }

            if insert_shaped:
                warned.append(entry)
                rec.warnings.append(
                    f"verify-applied: patch idx={idx} "
                    f"(applied_by={status!r}) contributes "
                    f"{len(missing)} of {len(added)} added line(s) "
                    f"that are not in the final content (first: "
                    f"{missing[0][:60]!r}).  Informational — the "
                    f"file is written; inspect the diff to confirm "
                    f"whether the patch's intent landed or was "
                    f"superseded by a later patch."
                )
                n_warned += 1
            elif len(missing) == len(added) and added:
                warned.append(entry)
                rec.warnings.append(
                    f"advisory: patch idx={idx} "
                    f"(applied_by={status!r}, splice-shaped) — all "
                    f"{len(added)} added line(s) are missing from "
                    f"the final content.  Informational; the file "
                    f"is written."
                )
                n_warned += 1
            # else: partial transform miss — byte-diff from a
            # legitimate splice.  Not warned, not recorded.

        if warned:
            rec.verification["patch_content_missing"] = warned
            print(f"  [verify-applied] {path}: "
                  f"{len(warned)} patch(es) with missing content "
                  f"lines (informational)",
                  file=sys.stderr)

    return n_warned
