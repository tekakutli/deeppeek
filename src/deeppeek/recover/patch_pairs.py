"""
patch_pairs — replace-pair application and drift tolerance.

A "pair" is two adjacent code blocks: an `old_body` and a `new_body`,
delimited by prose ("Find: ... Replace with: ...").  Detecting the
pair (which block is old, which is new) happens in `pairing.py`.
Applying the pair — replacing old_body with new_body in the base —
happens here.

The subtlety this module owns: **drift tolerance**.  A pair's intent
may already be reflected in the base after a previous application
or a manual edit.  `_pair_already_applied` uses fuzzy membership
(from patch_text) to recognise that case and reports it as a
successful no-op rather than a failure.  Fail-closed semantics are
preserved: a pair whose intent is *not* in base still fails.

Imports from patch_text, patch_scope, patch_diag, patch_heuristics.

PATH THREADING (2026-10-29)
===========================
`_apply_pairs` and `_try_replace_pair` now take an optional `path`.
It is forwarded to `_apply_scoped`, which uses it to pick the right
AST grammar when snapping splice boundaries (via `patch_bounds`) and
to run structural validation on the spliced content.  `_try_replace_pair`
also uses `path` to validate the fuzzy-replace and object-literal-splice
results before returning them — raw replace is safe by construction
(exact string match), but fuzzy is windowed and can be off by one line.

When `path` is empty (no extension known, e.g. an ad-hoc call from a
script), the boundary helpers degrade gracefully: AST snapping is
skipped, and `structural_valid` returns `True` for unrecognised
extensions.  The caller-visible behaviour is unchanged from the
pre-threading version.
"""

from .patch_text import _content_present_in_base
from .patch_scope import _apply_scoped
from .patch_diag import _diagnose_scoped_failure, _diagnose_replace_failure
from .patch_heuristics import (
    _try_object_literal_splice, _try_fuzzy_replace,
)


def _apply_pairs(base, pairs, path=""):
    """Apply each pair.  Each pair is independently crash-isolated:
    a raise inside one pair is recorded as that pair's failure and
    the loop continues with the next pair.
    """
    records = []
    current = base
    for pair in pairs:
        try:
            if pair["old_idx"] == -1:
                result = _apply_scoped(
                    current, pair["scope_name"], pair["new_body"],
                    path=path)
                if result is None:
                    reason = _diagnose_scoped_failure(
                        current, pair["scope_name"],
                        pair.get("new_body", ""))
                    records.append({"applied_by": "failed",
                                    "pair": pair, "reason": reason})
                    continue
            else:
                result = _try_replace_pair(
                    current, pair["old_body"], pair["new_body"],
                    path=path)
                if result is None:
                    reason = _diagnose_replace_failure(
                        current, pair["old_body"],
                        pair.get("new_body", ""))
                    records.append({"applied_by": "failed",
                                    "pair": pair, "reason": reason})
                    continue
            if result == current:
                records.append({"applied_by": "already-present",
                                "pair": pair})
                continue
            current = result
            records.append({"applied_by": "deterministic", "pair": pair})
        except Exception as _pair_exc:
            records.append({
                "applied_by": "failed",
                "pair": pair,
                "reason": (f"pair raised {type(_pair_exc).__name__}: "
                           f"{_pair_exc}"),
            })
    return current, records


# ===========================================================================
#  Already-applied / ambiguous predicates
# ===========================================================================

def _pair_already_applied(base, old, new):
    """True if the pair's post-state is already in base.

    Three cases, in order:

      1. old present + new present  → ambiguous, caller's other check
                                      (_pair_ambiguous) catches it
      2. old absent  + new present  → already applied (exact)
      3. old absent  + new mostly-present (fuzzy, token-equivalent)
                                    → already applied (drift: base
                                      rendered the post-state slightly
                                      differently)

    DRIFT-FIX: case 3 is the drift-tolerance addition.  It is why a
    pair whose base already has the post-state in a reordered or
    reformatted form is recognised as a successful no-op.  The
    "mostly-present" check uses `_content_present_in_base` at
    threshold=0.85, and requires that the old_body is NOT present
    (fuzzily) in base — that keeps the both-present ambiguity path
    routing to fail-closed as before.
    """
    if not new or not new.strip():
        return False
    if old and old in base:
        return False
    if new in base:
        return True
    if _content_present_in_base(base, new, threshold=0.85):
        if not old or not _content_present_in_base(base, old,
                                                   threshold=0.5):
            return True
    return False


def _pair_ambiguous(base, old, new):
    """True if both `old` and `new` bodies are present in base.

    This is genuinely ambiguous — could mean a partial prior
    application left the old block behind, or old appears as a
    comment / code example while new is the live code, or some
    other non-idempotent state.  Fail-closed so the record lands
    in low_trust/ and the user decides.
    """
    if not old or not old.strip():
        return False
    if not new or not new.strip():
        return False
    return old in base and new in base


# ===========================================================================
#  Object-literal prefix preservation for pairs
# ===========================================================================

def _try_object_literal_splice_for_pair(base, old_body, new_body):
    """Route a replace-pair whose new side is an object-literal
    assignment through `_try_object_literal_splice`, preserving the
    base's declaration prefix.

    Raw replace (`base.replace(old, new, 1)`) silently strips the
    base's leading declaration keyword.  The splice branch preserves
    the prefix by construction.
    """
    from .patch_heuristics import _OBJECT_LITERAL_ASSIGNMENT_RE

    new_stripped = new_body.strip()
    if not new_stripped:
        return None
    new_m = _OBJECT_LITERAL_ASSIGNMENT_RE.match(new_stripped)
    if not new_m:
        return None
    import re
    new_id_m = re.search(
        r'([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*=\s*$',
        new_m.group("prefix").rstrip(),
    )
    if not new_id_m:
        return None
    new_var = new_id_m.group(1)

    # Premise check -- same condition the raw-replace path requires.
    exact_match = old_body in base and base.count(old_body) == 1
    if not exact_match:
        def _norm(s):
            return "\n".join(ln.rstrip() for ln in s.splitlines())
        base_norm = _norm(base)
        old_norm = _norm(old_body)
        normalized_match = (old_norm in base_norm
                            and base_norm.count(old_norm) == 1)
        if not normalized_match:
            return None

    # Only fire when old and new name the same variable.  A rename
    # must NOT be routed here -- raw replace correctly handles it.
    old_stripped = old_body.strip()
    old_m = _OBJECT_LITERAL_ASSIGNMENT_RE.match(old_stripped)
    if old_m:
        old_id_m = re.search(
            r'([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*=\s*$',
            old_m.group("prefix").rstrip(),
        )
        if not old_id_m or old_id_m.group(1) != new_var:
            return None
    elif not re.search(
            r'\b' + re.escape(new_var) + r'\b', old_stripped):
        return None

    return _try_object_literal_splice(base, new_stripped, new_var)


# ===========================================================================
#  Replace-pair application
# ===========================================================================

def _try_replace_pair(base, old, new, path=""):
    """Apply a replace pair; return new content, base (already-present),
    or None (fail-closed).

    `path` is used only for post-splice structural validation.  Raw
    replace (`base.replace(old, new, 1)`) is safe by construction —
    the exact `old` string is present and gets swapped for `new`,
    which the AI produced as a replacement.  Fuzzy replace is windowed
    and can land off by one line; the structural check catches that.
    Object-literal splice is windowed too (the `[open, close]` of the
    literal); same check.
    """
    from .patch_text import _has_ellipsis_marker
    if not old.strip():
        return None

    # Ellipsis in old_body: cannot match literally.
    if _has_ellipsis_marker(old):
        return None

    # Drift: new is present, old is gone → already applied.
    if _pair_already_applied(base, old, new):
        return base

    # Both present: ambiguous.  Fail-closed.
    if _pair_ambiguous(base, old, new):
        return None

    def _validated(result):
        if result is None or result == base:
            return result
        if not path:
            return result
        from .patch_bounds import structural_valid
        if structural_valid(result, path):
            return result
        return None

    # Object-literal `new` body: route through the splice branch so
    # the base's const/let/var/export prefix is preserved.
    _spliced = _try_object_literal_splice_for_pair(base, old, new)
    if _spliced is not None:
        return _validated(_spliced)

    # Exact match (old must be unique).
    if old in base and base.count(old) == 1:
        result = base.replace(old, new, 1)
        if result != base:
            return _validated(result)

    # Whitespace-normalized (trailing-whitespace drift on base lines).
    def _norm(s):
        return "\n".join(ln.rstrip() for ln in s.splitlines())
    base_norm = _norm(base)
    old_norm = _norm(old)
    if old_norm in base_norm and base_norm.count(old_norm) == 1:
        pos = base_norm.find(old_norm)
        result = base_norm[:pos] + new + base_norm[pos + len(old_norm):]
        if result != base_norm:
            return _validated(result)

    # Fuzzy line-level match (token-equivalent lines, superset lines).
    fuzzy = _try_fuzzy_replace(base, old, new)
    if fuzzy is not None:
        return _validated(fuzzy)

    return None
