"""reverify — the verify-reconcile-verify loop.

Pass 8.5 (symbols) is the only remaining demoting pass on promoted
content.  Pass 8.4 (lint) and pass 8.4b (added-param lint) are
warn-only after the 2026-10-28 alignment — they surface "the
conversation was incomplete" but do not gate the write.

`reverify_reconciled` re-runs every pass that can affect trust on
every reconciled-and-promoted record.  If `pass8_5` still rejects the
promoted content, the record is re-demoted.  Lint passes are called
idempotently for warning parity, so a promoted record's warnings
reflect what a fresh pipeline run would have produced.

The invariant this module enforces:

    every pass that can change a record's trust must be re-run after
    every pass that can promote it, on the promoted content, before
    the promotion is committed.
"""
from __future__ import annotations


def reverify_reconciled(records, blocks_by_idx):
    """Re-run every demoting pass on reconciled-and-promoted records.

    Returns the number of records re-demoted to LOW.

    A record counts as "reconciled-and-promoted" iff it has the
    `reconciled` field in its verification AND its trust is not
    currently LOW.  LOW records were never promoted, so re-verifying
    them is a no-op — but skipping them saves the AST parse cost.
    """
    from .lint import (
        pass8_4_lint_command_args,
        pass8_4b_check_added_unused_params,
    )
    from .verify import pass8_5_check_symbols

    promoted = {
        path: rec for path, rec in records.items()
        if "reconciled" in rec.verification and rec.trust != "LOW"
    }
    if not promoted:
        return 0

    n_demoted = 0

    # Warn-only passes: called idempotently so warnings stay
    # consistent with what a fresh run would produce.  Their return
    # values are warning counts, not demotions.
    pass8_4_lint_command_args(promoted)
    pass8_4b_check_added_unused_params(promoted, blocks_by_idx)

    # Symbol re-check (demoting).
    pass8_5_check_symbols(promoted, blocks_by_idx=blocks_by_idx)

    for path, rec in promoted.items():
        if rec.trust == "LOW" and (
                "unresolved_calls" in rec.verification
                or "signature_mismatches" in rec.verification):
            n_demoted += 1
            if isinstance(rec.verification.get("reconciled"), dict):
                rec.verification["reconciled"]["post_reverify_demoted"] = True

    return n_demoted
