"""
recover.consistency — Pass 8.7d: verify `already-present` claims.

Motivation
==========
`pass6_apply_patches` marks a patch `already-present` when
`_try_deterministic_patch` returns its input unchanged.  The
dispatcher returns unchanged content in three situations:

  1. The body genuinely is already in base (a reprint; a splice whose
     target has no diff; a deletion whose lines are already gone).
  2. A heuristic returned `base` on a legitimate no-op transform.
  3. A heuristic *short-circuited* (`_is_already_applied` or
     `_patch_already_present` fired on a token-equivalence match) even
     though the body's actual content is **not** in base.

The first two are correct.  The third is a bug.

STRUCTURAL VERIFICATION (2026-11-06)
====================================
The pre-fix `_body_content_present()` was 95% line-equivalence. The
2026-10-30 run showed why that is too literal: patch 428's
`--output_rotation` dict entry was already in `comfyui.py`'s
`@command(args={...})` decorator, but the help-text wording differed
enough that line-equivalence said "not present," the pass flipped
the patch to `failed`, reconcile excluded it, and the file shipped
without the declaration.

Structural verification asks the right question: **is every
declaration the patch made present somewhere in the target?** For a
dict entry, that means the key. For a function/method, the name. For
a signature patch, the parameter name. For a decorator kwarg dict,
the kwarg key. These are all language-level structural concepts —
`ast_intent` extracts them uniformly across Python and JS.

The new check is:

  1. Fast path: exact substring.
  2. Line-equivalence at 95%.
  3. Structural intent — restricted.

STRUCTURAL FALLBACK IS NARROW ON PURPOSE (2026-11-06)
=====================================================
Two narrowing rules, each motivated by a real test in the suite:

  · RULE 1 — key-only bodies.  The fallback only fires when the
    body declares *nothing but* dict keys or decorator kwarg keys.
    If the body also declares a function, class, assignment, or
    parameter, the line-equivalence tier above is the right check
    for those shapes.  A function whose name matches but whose body
    differs is not "already present" — the whole function is the
    declaration, not its name.  (`body content partial below
    threshold` test.)

  · RULE 2 — no decls, no params.  Excluded from the structural
    fallback: any intent that mixes a key declaration with a
    `decl_names` or `param_names` declaration.  Those shapes need
    the full line-equivalence check to distinguish "the function
    exists somewhere in the file" from "the function's body is
    what the patch intended".  (`flips false already-present to
    failed` test.)

The full `intent_satisfied_in` in `ast_intent` is still the right
check for `pass8_11`'s warn-only verification; it's just too broad
to use as the sole evidence of "already present" in this pass.
"""

from __future__ import annotations

import sys
from typing import Optional

from .core import (
    FileRecord,
    _is_deletion_instruction,
    _looks_like_diff_body,
)


_ALREADY_PRESENT = "already-present"
_LINE_EQUIVALENCE_THRESHOLD = 0.95


def _body_content_present(body: str, content: str) -> bool:
    """True iff `body`'s content can reasonably be said to be in
    `content`.

    Three tiers:
      * Exact substring (after surrounding-whitespace strip).
      * Line-by-line stripped membership at 95%.
      * Structural intent — restricted.

    See the module docstring, STRUCTURAL FALLBACK IS NARROW ON
    PURPOSE, for why the structural tier only fires on pure
    key-declaration bodies.
    """
    stripped = body.strip()
    if not stripped:
        return True
    if stripped in content:
        return True
    body_lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not body_lines:
        return True
    content_lines = {ln.strip() for ln in content.splitlines()
                     if ln.strip()}
    matched = sum(1 for ln in body_lines if ln in content_lines)
    if matched / len(body_lines) >= _LINE_EQUIVALENCE_THRESHOLD:
        return True

    # Structural fallback — only for pure key-declaration bodies.
    try:
        from .ast_intent import extract_intent
        path = _path_of_current_record or ""
        body_intent = extract_intent(body, path)
        # RULE 1 + RULE 2: a body that also declares a function,
        # class, assignment, or param is more than a key-entry add.
        # Line-equivalence above is the right check for those
        # shapes; the structural tier does NOT fire.
        if body_intent.decl_names or body_intent.param_names:
            return False
        if not (body_intent.dict_keys
                or body_intent.decorator_kwarg_keys):
            return False
        target_intent = extract_intent(content, path)
        if (body_intent.dict_keys
                and not body_intent.dict_keys.issubset(
                    target_intent.dict_keys)):
            return False
        if (body_intent.decorator_kwarg_keys
                and not body_intent.decorator_kwarg_keys.issubset(
                    target_intent.decorator_kwarg_keys)):
            return False
        return True
    except Exception:
        pass
    return False


# The original file did not thread `path` through `_body_content_present`.
# Rather than change the signature (breaking every existing call), we
# thread the record path via a module-level slot. This mirrors the
# existing pattern for `_LAST_DEDUP_DETAILS` in validate_output.
_path_of_current_record: str = ""


def pass8_7d_verify_already_present(
        records: dict[str, FileRecord],
        blocks_by_idx: Optional[dict] = None,
        verbose: bool = False) -> int:
    """Verify every `already-present` patch's claim; flip false
    positives to `failed`.

    Returns the number of patches flipped to `failed`.
    """
    global _path_of_current_record

    n_flipped = 0
    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.is_deleted:
            continue
        if not rec.patches:
            continue
        _path_of_current_record = path

        for patch in rec.patches:
            if patch.get("applied_by") != _ALREADY_PRESENT:
                continue
            body = patch.get("body") or ""
            if not body.strip():
                continue
            if _looks_like_diff_body(body):
                continue
            if _is_deletion_instruction(
                    body, patch.get("context_before", "") or ""):
                continue
            if _body_content_present(body, rec.content):
                continue

            idx = patch.get("idx")
            patch["applied_by"] = "failed"
            patch["failure_reason"] = (
                "marked already-present but the body's structural "
                "intent (declared keys / names / params / decorator "
                "kwargs) and its line content are both absent from "
                "the current file; see consistency.py's module "
                "docstring"
            )
            rec.verification.setdefault(
                "already_present_unverified", []).append(idx)
            rec.warnings.append(
                f"already-present claim for patch idx={idx} was "
                f"contradicted by the file content; marked failed "
                f"(the patch's contribution is not in the target)"
            )
            rec.trust = "LOW"
            n_flipped += 1

            if verbose:
                print(f"  [8.7d] {path}: patch idx={idx} was marked "
                      f"already-present but its content is absent; "
                      f"flipped to failed", file=sys.stderr)

    _path_of_current_record = ""
    return n_flipped
