"""
patch_heuristics — aggregator for the apply heuristics.

This module is a stable aggregation facade.  The actual logic lives
in seven sibling sub-modules, sorted by churn stability:

  patch_predicates     pure predicates (very stable)
  patch_scope_insert   named-scope insertion (FIX 8, high churn)
  patch_anchor_insert  before/between/after-anchor (FIX 3, medium churn)
  patch_replace        single-line, object-literal, fuzzy (medium-stable)
  patch_misc_insert    context + object-entry insert (medium-stable)
  patch_structural     structural-append fallback (stable)
  patch_already        already-applied + diff predicates (very stable)

Every public and semi-public name from every sub-module is re-exported
here, so that `from deeppeek.recover.patch_heuristics import X` keeps
working unchanged for every X — including names that only exist in one
sub-module and names that are imported into a sub-module from
`patch_text` or `core` (those imports show up in `dir(sub-module)` and
are propagated by the loop below).

The split is deliberately invisible:

  · `patch_dispatch` imports 12 names from here; all resolve.
  · `patch_pairs` imports 2; all resolve.
  · `patch_diag` imports 1; resolves.
  · `apply_patches`'s `dir(patch_heuristics)` re-export loop copies
    the same non-`__` names it always has (superset of the pre-split
    set, since some sub-modules import additional core/patch_text
    helpers that were already in `dir(patch_heuristics)` before).

Churn isolation is the point of the split: a future edit to the
scope-insert heuristic touches ~300 lines, not ~1100.  A future edit
to a pure predicate touches ~150 lines with a wide blast radius
(every consumer sees it), which is exactly why those predicates live
together in their own stable file.
"""

from . import (
    patch_predicates,
    patch_scope_insert,
    patch_anchor_insert,
    patch_replace,
    patch_misc_insert,
    patch_structural,
    patch_already,
)

for _mod in (
    patch_predicates,
    patch_scope_insert,
    patch_anchor_insert,
    patch_replace,
    patch_misc_insert,
    patch_structural,
    patch_already,
):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_mod, _name)
del _mod, _name
