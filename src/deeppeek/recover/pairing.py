"""
recover.pairing — pair adjacent code blocks into replace edits.

After classify has assigned kinds and intents, this pass walks the
blocks in order and produces two kinds of pair:

  · **Replace pairs**: two adjacent blocks where the second's
    prose is a `with:`-style connective.  The patcher does
    `find(old) → replace(new)`.

  · **Scoped pairs**: a single block whose prose says
    "Replace the entire X for Y:" — the target is identified by
    name, not by content.  The patcher finds the named scope
    (HTML `<details>`, Python `def`, JS `function`, etc.) and
    replaces it.

A pair is *not* treated as a regular patch.  Both halves are
removed from the "trailing patch" path in dedup, so they don't
get re-applied as a delete / a stray snippet.

## Pair detection is shape-driven, not prose-driven

The primary signal is the SECOND block's prose: a connective
("with:", "to:", "becomes:", "new code:", "with the following").
That connective alone is enough.  The AI writing "with:" between
two adjacent code blocks is always describing "the block above is
the old version; the block below is the new version" — no verb
police is needed on the first block.

The first block's prose can be anything: "Replace the block:",
"Replace the doc comment above `buildContextMaskB64`:", "And
replace the function body:", "Here's the old version:", or nothing
at all.  Any shape that produces two adjacent partials with "with:"
between them is a replace.

This is the fix for the failure where "Replace the doc comment
above X:" did not match any entry in the old narrow
`_REPLACE_PROSE` list, so no pair formed, so the old comment block
was silently dropped and the new comment block was dropped too —
the file was written with the change half-applied.
"""

from .core import Block
from .prose import (
    parse_intent,
    is_with_connective,
    REPLACE_PAIR,
    REPLACE_SCOPED,
)


def detect_all(blocks: list[Block],
               blocks_by_idx: dict[int, Block]) -> None:
    """Populate `block.paired_with` on both halves of every pair."""
    _detect_replace_pairs(blocks, blocks_by_idx)
    _detect_scoped(blocks)


def _detect_replace_pairs(blocks: list[Block],
                          blocks_by_idx: dict[int, Block]) -> None:
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
        # Two adjacent code blocks separated by "with:" is the
        # replace-pair shape.  The connective is enough on its own —
        # the first block's prose does not need to match a specific
        # "replace" phrase.
        if is_with_connective(next_block.context_before):
            b.paired_with = next_block.idx
            next_block.paired_with = b.idx
            continue

        # Fallback: the first block's prose is a recognised
        # replace-verb AND the second block's prose signals a new
        # version.  Kept for prose variants the connective check
        # above does not cover (e.g. "Replace the block:" followed
        # by "New code:" without "with:").
        if parse_intent(b.context_before).action == REPLACE_PAIR:
            second_intent = parse_intent(next_block.context_before)
            if second_intent.action in (REPLACE_PAIR, "insert", "full"):
                b.paired_with = next_block.idx
                next_block.paired_with = b.idx


def _detect_scoped(blocks: list[Block]) -> None:
    for b in blocks:
        if getattr(b, "paired_with", None) is not None:
            continue
        if b.is_orphan:
            continue
        if b.kind not in ("partial", "full"):
            continue
        intent = parse_intent(b.context_before)
        if intent.action != REPLACE_SCOPED:
            continue
        if not intent.scope_name:
            continue
        b.scope_hint = intent.scope_hint
        b.scope_name = intent.scope_name
        b.paired_with = -1


def build_pairs(blocks_by_idx: dict[int, Block]) -> dict[int, dict]:
    """
    Return {anchor_idx: pair_dict} for every pair.

    Regular pairs: anchor_idx is the OLD block's idx.
    Scoped pairs:  anchor_idx is the NEW block's idx, `old_idx` = -1.
    """
    pairs: dict[int, dict] = {}
    for b in blocks_by_idx.values():
        paired = getattr(b, "paired_with", None)
        if paired is None:
            continue

        if paired == -1:
            # Scoped: single block, name in b.scope_name
            pairs[b.idx] = {
                "old_idx": -1,
                "new_idx": b.idx,
                "scope_hint": "scoped",
                "scope_name": b.scope_name,
                "new_body": b.body,
            }
            continue

        if paired < b.idx:
            # Second half of a pair; already recorded under old_idx
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
