"""
recover.pairing — pair adjacent code blocks into replace / scope edits.

After classify has assigned kinds and intents, this pass walks the
blocks in order and produces two kinds of pair:

  · **Replace pairs**: two adjacent blocks where the second's
    prose is a `with:`-style connective.

  · **Scoped pairs**: a single block whose prose says "Replace the
    X:" OR "Add X to Y:" OR contains any other replace/add verb,
    AND whose body carries a recognisable scope marker — an HTML
    `<details>` with a `<summary>`, a Python `def`, a JS `function`,
    a JS object literal.

A pair is *not* treated as a regular patch.  Both halves are
removed from the "trailing patch" path in dedup.

## Replace pairs are shape-driven

The primary signal for replace pairs is the SECOND block's prose:
a connective ("with:", "to:", "becomes:", "new code:", "with the
following").  That connective alone is enough.

## Scoped pairs fire on add verbs too

The add-family verbs ("add", "insert", "append", "prepend",
"include") often introduce a *full new version* of a scope rather
than a surgical insertion.  "Add the full-canvas early-out to
`drawInpaintContext`:" followed by a body that is the whole new
function is a scoped-replace in disguise.  The pairing pass treats
it as such: find the named scope in the baseline and replace it.
If the body IS an incremental insertion (no scope marker inside),
that falls through to `_try_context_insert` in `apply_patches`.
"""

import re

from .core import (
    Block,
    _is_deletion_instruction,
    _looks_like_diff_body,
)
from .prose import (
    parse_intent,
    is_with_connective,
    _extract_scope_from_body,
    _REPLACE_VERBS,
    _ADD_VERBS,
    REPLACE_PAIR,
    REPLACE_SCOPED,
)


_EXPORT_CONST_OBJECT_RE = re.compile(
    r'^export\s+(?:const|let|var)\s+\w+\s*=\s*\{'
)


def detect_all(blocks: list,
               blocks_by_idx: dict) -> None:
    """Populate `block.paired_with` on both halves of every pair."""
    _detect_replace_pairs(blocks, blocks_by_idx)
    _detect_scoped(blocks)


def _detect_replace_pairs(blocks: list,
                          blocks_by_idx: dict) -> None:
    for b in blocks:
        if getattr(b, "paired_with", None) is not None:
            continue
        if b.kind not in ("partial", "full"):
            continue

        next_block = blocks_by_idx.get(b.idx + 1)
        if next_block is None:
            continue
        if next_block.kind not in ("partial", "full"):
            continue
        if next_block.header_path != b.header_path:
            continue

        # Primary signal: the second block's prose is a connective.
        if is_with_connective(next_block.context_before):
            b.paired_with = next_block.idx
            next_block.paired_with = b.idx
            continue

        # Fallback: the first block's prose is a recognised
        # replace-verb AND the second block's prose signals a new
        # version.
        if parse_intent(b.context_before).action == REPLACE_PAIR:
            second_intent = parse_intent(next_block.context_before)
            if second_intent.action in (REPLACE_PAIR, "insert", "full"):
                b.paired_with = next_block.idx
                next_block.paired_with = b.idx


def _detect_scoped(blocks: list) -> None:
    for b in blocks:
        if getattr(b, "paired_with", None) is not None:
            continue
        if b.is_orphan:
            continue
        if b.kind not in ("partial", "full"):
            continue
        intent = parse_intent(b.context_before)

        # Explicit scoped replace: prose names the scope.
        if intent.action == REPLACE_SCOPED and intent.scope_name:
            b.scope_hint = intent.scope_hint
            b.scope_name = intent.scope_name
            b.paired_with = -1
            continue

        # If the prose matches a REPLACE_PAIR *without* a "with:"
        # connective, it's an old-half preamble.  Leave it for
        # `_detect_replace_pairs`.
        if (intent.action == REPLACE_PAIR
                and intent.scope_hint != "with"):
            continue

        # Fallback: the block has a replace OR add verb in prose
        # AND its body carries a scope marker AND the existing
        # patch machinery would not handle it.  Treat as scoped
        # replace.  "add" is included here because a body that IS
        # the full new scope is a scoped-replace regardless of the
        # verb the prose used.
        ctx_lower = b.context_before.lower()
        has_verb = (
            intent.action == REPLACE_PAIR  # "with:" prose without pairing
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


def _looks_like_patchable(b: Block) -> bool:
    """True if the existing patch machinery would handle this block.

    Prevents the scoped-replace fallback from stealing blocks that a
    deterministic patcher would process correctly (object literals,
    diffs, deletions).
    """
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
    first_line = body.split("\n", 1)[0].strip()
    if _EXPORT_CONST_OBJECT_RE.match(first_line):
        return True
    if body.startswith("@@ ") or "\n@@ " in body:
        return True
    return False


def build_pairs(blocks_by_idx: dict) -> dict:
    """
    Return {anchor_idx: pair_dict} for every pair.

    Regular pairs: anchor_idx is the OLD block's idx.
    Scoped pairs:  anchor_idx is the NEW block's idx, `old_idx` = -1.
    """
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
