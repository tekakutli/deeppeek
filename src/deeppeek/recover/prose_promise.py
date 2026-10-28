"""
recover.prose_promise — Pass 8.9: advisory for patches whose prose
promises call-site updates their fence body doesn't include.

Motivating case (comfyui.py, in the wild):

    And update the `_paste_result` signature and the three
    `paste_image(...)` / `paste_batch_as_layers(...)` call sites
    inside it so rotation is forwarded:

    [signature-only body]

The prose instructs *four* separate changes (signature plus three
call sites).  The fence only includes the signature.  Applied
verbatim, the result compiles but `output_rotation` is accepted and
silently discarded — the daemon-side rotation feature never fires.
The conversation authoring is at fault; the tool's job is to
surface it, not invent the missing code.

ADVISORY ONLY
=============
This pass emits a warning, never demotes a record.  Detecting
"prose promised more than the fence delivered" reliably requires
reading prose as instructions, and false positives would block
legitimate runs.  The warning names the missing work; whether that
blocks a merge is a human decision.

WHAT IT DETECTS
===============
Two prose shapes are recognized:

  Pattern A: `<fn chain> call sites`
      update the three `fn1(...)` / `fn2(...)` call sites
      change all `a(...)` / `b(...)` / `c(...)` call sites

  Pattern B: `call sites of <fn>`
      Change all 4 call sites of `foo`
      Update the call sites of `handler`
      Adjust call sites inside `parse_input`

For either pattern to fire, prose must ALSO contain an update-family
verb (`update`, `change`, `modify`, `adjust`, `rewrite`, `patch`,
`edit`) — this gate keeps descriptive prose like "the
`paste_image(...)` call sites are reviewed above" from triggering a
warning.

The two patterns exist because prose authors don't have a single
canonical shape.  `And update the `_paste_result` signature and the
three `paste_image(...)` / `paste_batch_as_layers(...) call sites`
puts the fn chain AFTER a count and an "and"; `Change all 4 call
sites of `foo`` puts the fn name AFTER the phrase.  A single
adjacency-requiring regex cannot match both.

WHY ROTATION-SPECIFIC
=====================
The check for "did the call-site patch land?" is rotation-specific:
the concrete kwarg `rotation=` in the promised call.  The name is a
much stronger signal than "any change" — a legitimate patch that
"updates the call sites" *will* contain
`some_fn(..., rotation=...)` if it actually does what the prose
claims.  Generalizing to arbitrary "promised change not in body"
requires a small prose DSL and would produce noise on the first
attempts; a future extension can add it once a few real cases
calibrate the patterns.

Aggregating across patches in the record — not just the patch whose
prose contains the promise — matters because the call-site patches
are separate blocks; their bodies collectively answer "did the
promised change land anywhere?".  Per-patch checking would warn on
every signature patch even when the call-site patch is also present.
"""

import re

from .core import FileRecord


# Update-family verbs.  Presence anywhere in the prose is required
# for either pattern to fire — see module docstring, WHAT IT DETECTS.
_UPDATE_VERB_RE = re.compile(
    r'\b(?:update|change|modify|adjust|rewrite|patch|edit)\b',
    re.IGNORECASE,
)

# Pattern A: backticked fn chain followed by `call sites`.
#   `paste_image(...)` / `paste_batch_as_layers(...)` call sites
# The `[^`\n]+` inside each backtick allows `fn(...)` or `fn` or
# `fn(arg1, arg2)`; extraction filters to just the identifier.
_FN_CHAIN_CALL_SITES_RE = re.compile(
    r'`[^`\n]+`(?:\s*/\s*`[^`\n]+`)*\s+call[- ]?sites?',
    re.IGNORECASE,
)

# Pattern B: `call sites` followed by `of`/`for`/`in`/`inside` and a
# backticked fn.
#   call sites of `foo`
#   call sites inside `parse_input`
_CALL_SITES_OF_FN_RE = re.compile(
    r'call[- ]?sites?\s+(?:of|for|in|inside)\s+`[^`\n]+`',
    re.IGNORECASE,
)

# Any backtick-enclosed token.
_BACKTICK_TOKEN_RE = re.compile(r'`([^`\n]+)`')


def _fn_from_token(tok):
    """Extract an identifier from a backtick token.

    `paste_image(...)` → `paste_image`
    `foo`              → `foo`
    `1 + 2`            → None (not identifier-shaped)
    """
    m = re.match(r'^\s*([A-Za-z_][\w]*)\s*(?:\(|$)', tok)
    return m.group(1) if m else None


def _promised_call_sites(prose):
    """Return the function names whose call sites the prose promises
    to update.  Empty when the prose doesn't promise call-site
    updates.

    Two prose shapes (see module docstring, WHAT IT DETECTS):
      · Pattern A: `<fn chain> call sites`
      · Pattern B: `call sites of/for/in/inside <fn>`

    Both require an update-family verb somewhere in the prose.  This
    gate prevents descriptive prose from firing.
    """
    if not prose:
        return []
    if not _UPDATE_VERB_RE.search(prose):
        return []

    fns = []

    # Pattern A: fn chain precedes `call sites`.
    for m in _FN_CHAIN_CALL_SITES_RE.finditer(prose):
        for tok_m in _BACKTICK_TOKEN_RE.finditer(m.group(0)):
            fn = _fn_from_token(tok_m.group(1))
            if fn and fn not in fns:
                fns.append(fn)

    # Pattern B: fn follows `call sites of`.
    for m in _CALL_SITES_OF_FN_RE.finditer(prose):
        for tok_m in _BACKTICK_TOKEN_RE.finditer(m.group(0)):
            fn = _fn_from_token(tok_m.group(1))
            if fn and fn not in fns:
                fns.append(fn)

    return fns


def _body_includes_call_updates(body, fn_names):
    """True iff `body` contains at least one call to one of
    `fn_names` with a `rotation=` kwarg.

    The kwarg name is what makes this check low-false-positive: a
    body that genuinely forwards rotation will contain
    `fn(..., rotation=...)` for each promised fn.
    """
    for fn in fn_names:
        if re.search(
                r'\b' + re.escape(fn) + r'\s*\([^)]*rotation\s*=',
                body, re.DOTALL):
            return True
    return False


def pass8_9_check_prose_promises(records):
    """Advisory: warn when prose promises call-site updates the
    fence body doesn't include.

    Returns the number of warnings emitted (one per record with any
    unmet promise).

    Never demotes a record.  Warn-only by design — see the module
    docstring, ADVISORY ONLY section.
    """
    n_flagged = 0
    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.is_deleted:
            continue

        promised_fns = set()
        patch_idxs = []
        for p in rec.patches:
            fns = _promised_call_sites(p.get("context_before", ""))
            if fns:
                promised_fns.update(fns)
                patch_idxs.append(p.get("idx"))

        if not promised_fns:
            continue

        all_bodies = " ".join(
            (p.get("body", "") or "") for p in rec.patches)

        missing = [
            fn for fn in sorted(promised_fns)
            if not _body_includes_call_updates(all_bodies, [fn])
        ]
        if not missing:
            continue

        rec.warnings.append(
            f"advisory: prose promises call-site updates to "
            f"{missing} but no patch body includes them "
            f"(patch idxs: {patch_idxs}) — the patch may accept a "
            f"new parameter without forwarding it to the promised "
            f"call sites"
        )
        n_flagged += 1

    return n_flagged
