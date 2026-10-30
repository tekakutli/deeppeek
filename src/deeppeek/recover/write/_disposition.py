"""Block-outcome classification for the manifest's `_debug` section.

MEDIUM CHURN.  `_compute_block_disposition` reads Block state and
emits a one-word summary (`applied`, `block_failed`,
`block_skipped_misattributed`, ...).  The vocabulary grows when a new
patch status or skip reason is added, which happens on the same
cadence as trust-logic changes.  Isolate it so `pass9.py`'s main
loop stays focused on writing decisions rather than labelling them.

TRUST-AWARE (2026-11-01e)
=========================
The pre-fix version classified a block as `applied` whenever the
record's *state* was `FULL_PLUS_PATCH`, even when the record was
trust-LOW and `written == False`.  The current logic checks the
record's trust, the write actually happening, and the block's own
patch status before claiming "applied".
"""

from ..core import Block


def _compute_block_disposition(b, records):
    """One-word summary of a block's fate.  Used in
    `manifest._debug.blocks`.

    Trust-aware: a block in a rejected-record previously showed as
    "applied" because the record's *state* was FULL_PLUS_PATCH; that
    misreads the outcome when trust was LOW and `written == False`.
    The disposition now reflects what actually happened:

      · `applied` — record written with this block's patch applied.
      · `block_skipped_misattributed` / `block_skipped_unplaceable` —
        pass 8.7 excluded the block and the rest of the record
        wrote cleanly.
      · `block_skipped_structural_damage` — the structural sanity
        gate refused the patch; see
        apply_patches._check_structural_sanity.
      · `block_failed` — patch failed and no reconciliation could
        recover the record.
      · `block_unapplied` — record fail-closed; block's patch may
        have applied but the file was not written.
      · `block_partial` — pair member in a record that was not
        written.
    """
    if getattr(b, "disposition", None):
        return b.disposition
    if b.classified_by == "preexisting":
        return "preexisting"
    if b.is_orphan:
        if b.attributed_to:
            rec = records.get(b.attributed_to)
            if not rec or rec.state not in (
                    "SINGLE_FULL", "FULL_PLUS_PATCH",
                    "MULTI_FULL"):
                return "orphan_attributed_skipped"
            _own_patch = next(
                (p for p in rec.patches
                 if p.get("idx") == b.idx),
                None)
            if (_own_patch is not None
                    and _own_patch.get("applied_by")
                    in ("deterministic", "llm")):
                return "orphan_attributed_applied"
            return "orphan_attributed_skipped"
        if b.kind == "not_a_file":
            return "orphan_not_code"
        if b.kind == "recovered_meta":
            return "orphan_meta"
        if b.attributed_by == "empty":
            return "orphan_empty"
        if b.attributed_by == "blockquote_skip":
            return "blockquote_skip"
        return "orphan_unattributed"
    if getattr(b, "paired_with", None) is not None:
        # Pair member: report the record-level outcome so a pair in
        # a written record is distinguishable from one in a
        # skipped record.
        rec = records.get(b.output_path) if b.output_path else None
        if rec is None:
            return "paired"
        if rec.trust == "LOW" and not getattr(rec, "_was_written", False):
            return "block_partial"
        return "paired"
    if b.output_path is None:
        return "unresolved"
    rec = records.get(b.output_path)
    if rec is None:
        return "unmatched"
    if rec.state not in (
            "SINGLE_FULL", "FULL_PLUS_PATCH", "MULTI_FULL"):
        if rec.state == "PARTIAL_ONLY":
            return "partial"
        return "unapplied"

    # Look at the block's own patch status first — this is what
    # reconciliation changes.
    _own = next(
        (p for p in rec.patches if p.get("idx") == b.idx),
        None)
    if _own is not None:
        status = _own.get("applied_by")
        if status == "skipped_misattributed":
            return "block_skipped_misattributed"
        if status == "skipped_unplaceable":
            return "block_skipped_unplaceable"
        if status == "skipped_structural_damage":
            return "block_skipped_structural_damage"
        if status == "failed":
            return "block_failed" if rec.trust == "LOW" else "applied"

    if rec.trust == "LOW" and not getattr(rec, "_was_written", False):
        return "block_unapplied"
    if getattr(rec, "sanity_rejected", False):
        return "block_unapplied"
    return "applied"
