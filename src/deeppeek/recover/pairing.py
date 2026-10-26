"""
recover.pairing — pair adjacent code blocks into replace / scope edits.

PAIR DETECTION
==============
Two blocks are paired as (old, new) when:

    1. They are adjacent, in the same file, and both `partial`/`full`.
    2. The *old* side is NOT already classified as "new" by prose
       (a "with:"-block cannot also be an "old"-block).
    3. The *new* side is classified "new" by prose, OR the old side
       is classified "old" and the two bodies share enough lines to
       look like versions of each other.
    4. The new side is not itself a scoped-replace candidate (body
       carries a scope marker AND prose introduces a scoped replace).

SCOPED DETECTION
================
A single block becomes a scoped-replace (`paired_with = -1`) when:

    1. Its prose names the scope (`_SCOPED_REPLACE_RE` matches), OR
    2. Its prose says "replace … with:" AND its body carries a scope
       marker (name extracted from body via
       `_extract_scope_from_body`), OR
    3. Its body carries a scope marker AND its prose has a
       replace/add verb.

Case 2 is the fix for "Replace the entire Inpainting `<details>` … with:"
when the scope name is only in the body.
"""

import re

from .core import (
    Block,
    _is_deletion_instruction,
    _looks_like_diff_body,
)
from .prose import (
    parse_intent,
    is_new_block_prose,
    is_old_block_prose,
    is_scoped_replace_by_body,
    _extract_scope_from_body,
    _REPLACE_VERBS,
    _ADD_VERBS,
    REPLACE_PAIR,
    REPLACE_SCOPED,
)


# Object-literal assignments: `export const X = {`, `const X = {`,
# `let X = {`, `var X = {`, `X = {`, `a.b.c = {`.  These bodies are
# handled by `_try_object_literal_splice` in apply_patches.py, which
# preserves the base's leading prefix (const / let / var /
# export const).  They must NOT be routed through `_apply_scoped`
# (scoped-replace strips the base prefix) or through replace-pairs
# whose old side has a prefix the new side omits (raw replace also
# strips it).
#
# Superset of the previous _EXPORT_CONST_OBJECT_RE: it additionally
# matches `const X = {`, `let X = {`, `var X = {`, `X = {`, and
# `a.b.c = {`.  Every case the old pattern matched is still matched
# here, so callers that used to see True still see True.
_OBJECT_LITERAL_ASSIGNMENT_RE = re.compile(
    r'^(?P<prefix>'
    r'(?:export\s+)?'
    r'(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*='
    r'|'
    r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*='
    r')'
    r'\s*\{'
)


# ---------------------------------------------------------------------------
# Body-version similarity — used to safely pair (old, neutral)
# ---------------------------------------------------------------------------

def _bodies_look_like_versions(a: str, b: str) -> bool:
    """True if two bodies plausibly represent old and new versions of
    the same scope.

    Two independent signals:
      · similar size (within 3×)
      · at least one shared "meaningful" line (≥10 chars).

    Both are deliberately loose.  False positives are caught by the
    patcher (the pair will simply fail to apply); false negatives
    leave the pair unpaired, which is safer than pairing unrelated
    blocks."""
    if not a or not b:
        return False
    a_lines = [ln.strip() for ln in a.splitlines() if ln.strip()]
    b_lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
    if not a_lines or not b_lines:
        return False
    if (len(a_lines) / len(b_lines) > 3.0
            or len(b_lines) / len(a_lines) > 3.0):
        return False
    common = {ln for ln in (set(a_lines) & set(b_lines)) if len(ln) >= 10}
    return bool(common)


# ---------------------------------------------------------------------------
# Replace-pair detection
# ---------------------------------------------------------------------------

def _detect_replace_pairs(blocks: list, blocks_by_idx: dict) -> None:
    for i in range(len(blocks) - 1):
        b = blocks[i]
        next_block = blocks[i + 1]

        # Thought-process code (blockquoted by deeppeek's formatter
        # or by the source GUI) is exploratory reasoning, not the
        # assistant's committed answer.  Pairing such fragments --
        # either with each other or with a real patch -- produces
        # false pairs whose old_body references a base state that
        # was never committed.  They fail closed (correct safety),
        # but they add noise to the manifest and dilute the real
        # failures.  Skip blockquoted blocks from pair detection
        # entirely.
        if b.in_blockquote or next_block.in_blockquote:
            continue

        if getattr(b, "paired_with", None) is not None:
            continue
        if getattr(next_block, "paired_with", None) is not None:
            continue
        if b.kind not in ("partial", "full"):
            continue
        if next_block.kind not in ("partial", "full"):
            continue
        if b.classified_by == "preexisting":
            continue
        if next_block.classified_by == "preexisting":
            continue
        if next_block.header_path != b.header_path:
            continue

        b_new = is_new_block_prose(b.context_before)
        b_old = is_old_block_prose(b.context_before)
        next_new = is_new_block_prose(next_block.context_before)
        next_old = is_old_block_prose(next_block.context_before)

        # A block whose own prose is "new" is the new side of the pair
        # above it.  It cannot also be the old side of the pair below.
        if b_new:
            continue

        # The next block declared itself "old" — it can't be a new side.
        if next_old:
            continue

        # Primary signal: next is explicitly new.
        if next_new:
            # Defer to scoped-replace if the next block's prose names
            # a scope AND its body carries a scope marker.  Otherwise
            # this is a straightforward pair.
            if is_scoped_replace_by_body(
                    next_block.context_before, next_block.body):
                continue
            b.paired_with = next_block.idx
            next_block.paired_with = b.idx
            continue

        # Fallback: b is explicitly old, next is neutral, and the
        # two bodies look like versions of each other.
        if b_old and _bodies_look_like_versions(b.body, next_block.body):
            b.paired_with = next_block.idx
            next_block.paired_with = b.idx
            continue


# ---------------------------------------------------------------------------
# Scoped detection
# ---------------------------------------------------------------------------

def _detect_scoped(blocks: list) -> None:
    for b in blocks:
        if getattr(b, "paired_with", None) is not None:
            continue
        if b.is_orphan:
            continue
        if b.kind not in ("partial", "full"):
            continue
        if b.classified_by == "preexisting":
            continue

        intent = parse_intent(b.context_before)

        # Case 1: prose names the scope with "for"/"named".
        if intent.action == REPLACE_SCOPED and intent.scope_name:
            b.scope_hint = intent.scope_hint
            b.scope_name = intent.scope_name
            b.paired_with = -1
            continue

        # Case 2: prose says "replace … with:" AND body carries a
        # scope marker.  The name comes from the body.
        if is_scoped_replace_by_body(b.context_before, b.body):
            if _looks_like_patchable(b):
                # Object-literal assignments route to
                # `_try_object_literal_splice` (preserves base prefix),
                # not `_apply_scoped` (strips it).  See
                # _looks_like_patchable.
                continue
            name, kind = _extract_scope_from_body(b.body)
            b.scope_hint = kind
            b.scope_name = name
            b.paired_with = -1
            continue

        # Old-side pair halves must be left alone for
        # _detect_replace_pairs.
        if (intent.action == REPLACE_PAIR
                and intent.scope_hint != "with"):
            continue

        # Case 3: prose has a replace/add verb AND body carries a
        # scope marker AND the block would not be handled by the
        # patch machinery as-is.
        ctx_lower = b.context_before.lower()
        has_verb = (
            intent.action == REPLACE_PAIR
            or any(v in ctx_lower for v in _REPLACE_VERBS)
            or any(v in ctx_lower for v in _ADD_VERBS)
        )
        if not has_verb:
            continue
        if _looks_like_patchable(b):
            continue
        name, kind = _extract_scope_from_body(b.body)
        if not name:
            continue
        b.scope_hint = kind
        b.scope_name = name
        b.paired_with = -1


# ---------------------------------------------------------------------------
# Patchability pre-check (used to prevent scoped detection from
# swallowing blocks the deterministic patcher would handle)
# ---------------------------------------------------------------------------

def _looks_like_patchable(b: Block) -> bool:
    body = b.body.strip()
    if not body:
        return False
    if b.override_reason == "recovered-meta listing":
        return False
    if b.override_reason and (
            "diff-context anchor" in b.override_reason
            or "reprint-context anchor" in b.override_reason):
        return True
    if _is_deletion_instruction(body, b.context_before):
        return True
    if b.fence_lang.lower() == "diff":
        return True
    if _looks_like_diff_body(body):
        return True
    # Object-literal assignments (const/let/var, export-prefixed,
    # plain, dotted) are handled by `_try_object_literal_splice`,
    # which preserves the base's leading prefix.  Treat them as
    # patchable so `_detect_scoped` does not route them through
    # `_apply_scoped` (which strips the prefix).
    if _OBJECT_LITERAL_ASSIGNMENT_RE.match(body):
        return True
    if body.startswith("@@ ") or "\n@@ " in body:
        return True
    return False


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def detect_all(blocks: list, blocks_by_idx: dict) -> None:
    """Populate `block.paired_with` on both halves of every pair."""
    _detect_replace_pairs(blocks, blocks_by_idx)
    _detect_scoped(blocks)


def build_pairs(blocks_by_idx: dict) -> dict:
    """Return {anchor_idx: pair_dict} for every pair."""
    pairs: dict = {}
    for b in blocks_by_idx.values():
        paired = getattr(b, "paired_with", None)
        if paired is None:
            continue
        if paired == -1:
            pairs[b.idx] = {
                "old_idx": -1,
                "new_idx": b.idx,
                "scope_hint": "scoped",
                "scope_name": b.scope_name,
                "new_body": b.body,
            }
            continue
        if paired < b.idx:
            continue
        other = blocks_by_idx.get(paired)
        if other is None:
            continue
        pairs[b.idx] = {
            "old_idx": b.idx,
            "new_idx": paired,
            "scope_hint": "replace",
            "old_body": b.body,
            "new_body": other.body,
        }
    return pairs
