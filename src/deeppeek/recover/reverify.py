"""reverify — the verify-reconcile-verify loop.

Pass 8.4 (lint) and pass 8.5 (symbols) both demote records.  Pass
8.7 (reconcile) promotes records by excluding misattributed items.
Without re-running 8.4/8.5 after 8.7, reconcile can commit a
promotion that one of the earlier demotions was warning about — the
promoted content is different from the pre-exclusion content, so
each demoting pass must re-run on the promoted content.

Fix 2 (see `reconcile.py`) makes `_count_failures` include lint, so
the *specific* case is caught inside pass 8.7 itself.  This module
is the *general* safety net: any future demoting pass added to the
pipeline is automatically re-applied to reconciled records, even if
a maintainer forgets to update `_count_failures`.

The invariant:

    every pass that can demote a record must be re-run after every
    pass that can promote it, on the promoted content, before the
    promotion is committed.

`pipeline.py` calls `reverify_reconciled` immediately after
`pass8_7_reconcile` returns.  The order matters: reconcile must
have run so `rec.verification["reconciled"]` is populated; reverify
must run before any later pass (like the 8.8 shield) that could
re-trust the record.
"""
from __future__ import annotations


def reverify_reconciled(records, blocks_by_idx):
    """Re-run every demoting pass on reconciled-and-promoted records.

    Returns the number of records re-demoted to LOW.

    A record counts as "reconciled-and-promoted" iff it has the
    `reconciled` field in its verification AND its trust is not
    currently LOW.  LOW records were never promoted (reconcile
    correctly refused to promote them), so re-verifying them is a
    no-op — but skipping them saves the AST parse cost.
    """
    from .lint import pass8_4_lint_command_args
    from .validate import pass8_5_check_symbols

    promoted = {
        path: rec for path, rec in records.items()
        if "reconciled" in rec.verification and rec.trust != "LOW"
    }
    if not promoted:
        return 0

    n_demoted = 0

    # Lint re-check.  A promoted record whose @command declares an
    # unused arg is still broken; demote again so it stays LOW.
    n_demoted += pass8_4_lint_command_args(promoted)

    # Symbol re-check.  This runs in-place on `promoted` (the same
    # dict as `records` for the promoted keys); `pass8_5_check_symbols`
    # iterates `records.items()` and mutates each record's `trust`
    # directly, so no return value is needed.
    pass8_5_check_symbols(promoted, blocks_by_idx=blocks_by_idx)

    # pass8_5 doesn't return a count; count by inspecting the result.
    for path, rec in promoted.items():
        if rec.trust == "LOW" and (
                "unresolved_calls" in rec.verification
                or "signature_mismatches" in rec.verification):
            n_demoted += 1
            if isinstance(rec.verification.get("reconciled"), dict):
                rec.verification["reconciled"]["post_reverify_demoted"] = True

    return n_demoted
