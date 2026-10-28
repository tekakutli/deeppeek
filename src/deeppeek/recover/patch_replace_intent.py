"""Detect whether a patch body REPLACES an existing base block
rather than INSERTING new content.

Why this is a separate heuristic: `_filter_body_by_blocks` filters
out body lines that already appear in the base, on the theory that
they are "context."  That is correct when the patch is inserting new
content.  It is WRONG when the patch is a modified version of a base
block — the filtered body loses the anchor lines that prove it's a
replacement, and the dispatcher then treats the residue as a new
addition.

Motivating case (2026-10-29 pg_panel_scrubs.py run): the patch
starts with two base-identical lines followed by one new line and a
modified function.  Filtered, the body began with the new line
(`const rotationInputEl`), which the AST anchor heuristic found a
valid insertion point for.  The old `if (rotationEl) {…}` block, the
old `pgkitRegisterField("view.rotation", …)` call, and the unused
`_syncRotationWidget` all stayed in the file — duplicate registrations,
duplicate event listeners, dead code.

`detect_replace_intent` runs BEFORE `_filter_body_by_blocks`.  If
the unfiltered body diffs against the best-matching base region
with a meaningful `replace` or `delete` opcode (of non-blank
content), the patch is a replacement; the filter step is skipped and
the full body is used.

BLANK-LINE ALIGNMENT (2026-11-05 fix)
=====================================
An earlier version returned a valid replacement for patches whose
match was anchored at a leading blank line.  Concrete failure
(tests/recover/test_recover.py::test_02 and ::test_04):

    base:
        const PAD_28 = 28;
        <blank>
        export const foo = {
          a: 1,
          b: 2,
        };

    patch:
        export const foo = {
          a: 1,
          b: 2,
          c: 3,
        };

    The best 5-line window was [blank, export..., a, b, };] because
    `range(len(base_lines) - 5 + 1)` stops at the 5-line window whose
    slice ends *past* the file.  SequenceMatcher produced a
    `delete('')` opcode for the leading blank; the fired replacement
    consumed that blank as part of the region, and the written file
    was missing the separator line.

Two mitigations, both applied here:

  · Leading and trailing blank lines are stripped from each candidate
    window before comparison.  `start` is shifted by the number of
    stripped leading blanks; `end = start + len(window_stripped)`.
    This aligns the window's content boundary with the patch's
    (already stripped) content boundary and removes the source of the
    spurious `delete('')`.

  · Even if a `delete` or `replace` opcode survives, it only counts as
    replace-intent when the affected base lines are non-blank, or (for
    `replace`) the incoming patch lines are non-blank.  A `delete('')`
    of an empty line is never evidence that the surrounding block was
    replaced.

The additive case is unaffected: a patch whose best diff contains only
`insert` opcodes still returns None and falls through to
`_try_deterministic_patch`.
"""
import difflib


_MIN_MATCH_RATIO = 0.30
_MIN_CONTIGUOUS = 4


def detect_replace_intent(base: str, patch_body: str):
    if not base or not patch_body:
        return None

    patch_lines = patch_body.split("\n")
    patch_norm = [ln.rstrip() for ln in patch_lines]
    while patch_norm and not patch_norm[-1].strip():
        patch_norm.pop()
    while patch_norm and not patch_norm[0].strip():
        patch_norm.pop(0)
    if len(patch_norm) < _MIN_CONTIGUOUS:
        return None

    base_lines = base.split("\n")

    # Factors below 1.0 matter when the patch expands the region it
    # replaces: a rewrite that adds a helper, an `if (…)` branch, or a
    # second registration is longer than the base region it should
    # consume.  Without sub-unit factors, the only windows tried are
    # ≥ patch length, so a 50-line patch cannot match a 37-line base
    # region, the ratio drops (because the window pulls in trailing
    # content that the patch doesn't), and the pattern-match never
    # fires — the patch falls through to an insert heuristic and
    # produces duplicate top-level declarations.
    best = None
    for window_factor in (0.5, 0.7, 0.85, 1.0, 1.15, 1.5, 2.0):
        window_size = int(len(patch_norm) * window_factor)
        if window_size < _MIN_CONTIGUOUS:
            continue
        for start in range(len(base_lines) - window_size + 1):
            window = base_lines[start:start + window_size]

            ws = 0
            while ws < len(window) and not window[ws].strip():
                ws += 1
            we = len(window)
            while we > ws and not window[we - 1].strip():
                we -= 1
            window_stripped = window[ws:we]
            if len(window_stripped) < _MIN_CONTIGUOUS:
                continue

            # The patch and the base region must agree on their first
            # non-blank line — otherwise the "match" is a mid-region
            # fragment and the replacement would delete live content
            # the patch never saw.
            if window_stripped[0].strip() != patch_norm[0].strip():
                continue

            sm = difflib.SequenceMatcher(
                None, window_stripped, patch_norm, autojunk=False)
            ratio = sm.ratio()
            if best is None or ratio > best[0]:
                best = (ratio, start + ws, we - ws, sm, window_stripped)

    if best is None or best[0] < _MIN_MATCH_RATIO:
        return None

    ratio, start, length, sm, window_stripped = best

    opcodes = sm.get_opcodes()

    has_meaningful_change = False
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if tag == "replace":
            if (any(ln.strip() for ln in window_stripped[i1:i2])
                    or any(ln.strip() for ln in patch_norm[j1:j2])):
                has_meaningful_change = True
                break
        elif tag == "delete":
            if any(ln.strip() for ln in window_stripped[i1:i2]):
                has_meaningful_change = True
                break
    if not has_meaningful_change:
        return None

    matched_lines = sum(i2 - i1 for tag, i1, i2, _, _ in opcodes
                        if tag == "equal")
    if matched_lines < _MIN_CONTIGUOUS:
        return None

    return start, start + length, "\n".join(patch_norm)
