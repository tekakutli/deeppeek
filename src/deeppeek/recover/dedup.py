"""
recover.dedup — Pass 5 (pick one full-print winner per file) plus
pair detection.

Pair detection runs first.  Any block that is half of a replace pair
or a scoped replace is *removed* from the normal trailing-patch path
— it becomes a structured edit that `apply_patches` handles
separately.

PAIR VALIDATION AGAINST THE BASE
================================
Not every `paired_with = -1` block is a valid scoped replace.  Two
cases are rejected here, and the block falls through to the
trailing-patch path:

  1. The pair targets the chosen full-print block itself.  A block
     that IS the base cannot be a scoped replace *of* itself — the
     result would nest the block inside itself (the pg_state.py
     failure mode).

  2. The scope named by the pair does not exist in the base.  This
     is an insert of a NEW scope, not a replace of an existing one.
     `_apply_scoped` correctly fails, but the pair is recorded as a
     failure and drags the record to LOW for no good reason.  The
     block instead goes through `_try_context_insert` or
     `_try_insert_after_anchor`, where "insert between X and Y" prose
     can actually land.

CROSS-FILE DUPLICATE ATTRIBUTION
================================
An orphan blockquoted block attributed by prose can be misattributed
— the prose names a file, but the content belongs to a DIFFERENT
file's pre-existing source.  The pg_inpaint.py failure: a 4-line
fragment from `_inpaintAction` was attributed to `pg_inpaint.py`
because the prose mentioned "inpaint result", while the fragment was
already verbatim in `pg_panel_bindings.py`.  We check: if the
fragment is already present in a DIFFERENT pre-existing record, the
attribution was a false positive; skip the block.

SILENT-DROP POLICY (2026-11-01c)
================================
Any attributed partial block whose body shows a code signal becomes
a patch candidate.  The code-signal check (`_has_code_signal`) is
intentionally permissive: it accepts `=`, `;`, `{`, `}`, `=>`,
`->`, `identifier(`, `: value`, HTML tags, and a small set of
code keywords.  This is the general-purpose rule:

    if a block was attributed to a file (by header or by orphan
    attribution) and its body contains anything a programmer would
    write as code, we attempt the patch.

If the patch cannot apply, fail-closed catches it: the record goes
to `low_trust/` and the target is preserved.  This is strictly
better than the previous policy, which silently dropped legitimate
code additions (blocks 41, 42, 44 in the pg_panel_fields /
pg_panel_scrubs edits were dropped because they contained
single-line bodies without an add-family verb in the prose).

The code-signal check only excludes genuinely prose-y blocks
(`[Inpaint] [Cancel]`, `Drag the rectangle to move it...`, bare
sentences).  Those would have been mis-classified as `partial` by
the heuristic classifier; the code-signal check filters them before
they reach the patcher.

SUBSET-DEMOTE POLICY (2026-11-03)
=================================
A conversation "full" print that is a strict substring of a
preexisting file is not a full replacement — it is a function (or
a cluster of functions) extracted from the preexisting.  The
pg_state.py case is the canonical example: the preexisting file is
`STATE_JS = r\"\"\"...\"\"\"` with a 17-line docstring above it, and
the conversation reprints just `STATE_JS = r\"\"\"...\"\"\"` (no
docstring, no surrounding module-level code).

When a record has BOTH a preexisting full and a conversation
"full" and the conversation body is a substring of the preexisting
body, demote the conversation block to a patch and keep the
preexisting as the base.  Without this, the patcher writes the
fragment + subsequent patches against an incomplete base, and the
resulting file is missing imports and sibling helpers.  The
specific failure observed in the wild: `kritomatic_daemon/utils/
comfyui_krita.py` where the conversation printed only
`paste_image` as `full`, the preexisting full was discarded, and
`paste_batch_as_layers` was applied to a base with no imports or
`_decode_png` — resulting in a file that fail-closes on first
`ast.parse`.

COVERAGE-DEMOTE POLICY (2026-11-05)
===================================
Subset-demote only fires when the real body is literally contained
in (or token-equivalent to) the preexisting body.  That misses the
case where a conversation "full" print is a *rewrite* of a small
subset of the preexisting file's declarations — the `paste_image`
block in `comfyui_krita.py` (1 function of 22) is a rewrite of
`paste_image`, not a substring of it (it adds `rotation`,
`_rotate_about_center`, `_center_crop`).

The fix: count how many of the pre-existing file's top-level
declarations the conversation body declares.  If the conversation
body declares <50% of them, it cannot be a full-file replacement;
demote to a patch and use the preexisting as the base.  Scope-aware
replacement then handles each function individually, and the
preexisting declaration set survives intact.

For the two motivating cases:
  · pg_state.py:     1 of ~5 declarations = 0.2  → demote
  · comfyui_krita.py: 1 of 22     = 0.045 → demote

For a legitimate full-file rewrite that preserves most functions:
coverage ≥0.5 → keep as full replacement (existing behaviour).

EMBEDDED-FRAGMENT RECLASSIFICATION (2026-11-01b)
================================================
Pass 2's heuristic classifier falls back to
`_looks_like_full_file(body, "unknown_N")` when no header path is
present.  The non-`.py` branch of `_looks_like_full_file` returns
`True` on any non-empty body, so a headerless JS/HTML/CSS fragment
arrives at pass 5 marked `kind = "full"`.  That label is meaningless
once pass 4 has attributed the block to a pre-existing file — a `.py`
file cannot become a `js` body, so the fragment is a partial to be
inserted or spliced, not a replacement.

`pass5_deduplicate` now demotes `full`-kind orphans whose fence
language is JS/HTML/CSS and whose attributed target is a pre-existing
`.py` file.  Combined with the parallel change that allows `full`-kind
orphans into the `orphan_patches` collection, this is what makes
`pg_base.py`, `pg_panel_scrubs.py`, and `pg_panel_bindings.py` fixes
actually apply after attribution.  Before this, the `full` label
caused them to be silently dropped from the patch pipeline.
"""

import re
import sys
from typing import Optional

from .core import (
    Block, FileRecord, FIX_PROSE_PHRASES, MODIFIER_RANK,
    _is_deletion_instruction,
    _looks_like_diff_body,
)
from .pairing import detect_all, build_pairs
from .patch_scope import _scope_in_base


_EXPORT_CONST_OBJECT_RE = re.compile(
    r'^export\s+(?:const|let|var)\s+\w+\s*=\s*\{'
)


# ===========================================================================
#  Embedded-fragment language set (2026-11-01b)
# ===========================================================================
#
# Languages whose fragments are commonly embedded inside a `.py`
# file's string literal (playgrounds, template systems).  A `full`-
# kind orphan with one of these fence languages, attributed to a
# pre-existing `.py` file, is demoted to `partial` in
# `pass5_deduplicate`.  See the module docstring, EMBEDDED-FRAGMENT
# RECLASSIFICATION section.

_EMBEDDED_BLOCK_LANGS = frozenset({
    "js", "mjs", "jsx", "ts", "tsx",
    "html", "css", "scss",
})


# ---------------------------------------------------------------------------
# Code-signal detection
# ---------------------------------------------------------------------------
#
# A "code signal" is anything that distinguishes code from prose:
#
#   * Assignment, statement terminator, or block braces anywhere on a line
#   * Arrow function or type hint
#   * An identifier followed by `(` (function/method call)
#   * A key: value pattern (object literal entry)
#   * An HTML/XML opening tag
#
# Line-start keyword detection covers the remaining common cases
# (`def`, `class`, `function`, `const`, `let`, `var`, `import`,
# `from`, `return`, `async`, `await`, `yield`, `try`, `except`,
# `finally`, `throw`, `raise`, `export`, `package`).
#
# `if`, `for`, `while`, `else`, `elif`, `with` are deliberately NOT
# in the keyword list — they are too common as prose words.  A line
# starting with `if (` will still pass because of the `(` signal.

_CODE_KEYWORDS = (
    "def ", "class ", "function ", "const ", "let ", "var ",
    "import ", "from ", "return ", "async ", "await ", "yield ",
    "try ", "except ", "finally ", "throw ", "raise ",
    "export ", "package ",
)

_CODE_PUNCT_LINE_RE = re.compile(
    r'[=;]'                       # assignment or statement terminator
    r'|[{}]'                      # block braces
    r'|=>'                        # arrow function
    r'|->\s'                      # type hint (Python/TS)
    r'|\(\s*\)'                   # empty call
    r'|[A-Za-z_$][\w$]*\s*\('     # identifier followed by (
    r'|:\s+\S'                    # key: value pattern
    r'|<\/?[A-Za-z]'              # HTML/XML tag
)

_TOKEN_RE = re.compile(
    r'[A-Za-z_$][\w$]*|[0-9]+|"[^"]*"|\'[^\']*\'|`[^`]*`'
)


def _meaningful_tokens(text):
    """Identifier-and-literal tokens of length >= 4, excluding pure digits."""
    if not text:
        return set()
    return {
        t for t in _TOKEN_RE.findall(text)
        if len(t) >= 4 and not t.isdigit()
    }


def _top_level_decls(text):
    """Names declared at top level by def/function/class/const/let/var."""
    if not text:
        return set()
    return set(re.findall(
        r'^(?:export\s+)?(?:async\s+)?'
        r'(?:def|function|class|const|let|var)\s+([A-Za-z_$][\w$]*)',
        text, re.MULTILINE,
    ))


def _token_subset(real_body, pre_body):
    """True iff the real body's meaningful tokens are a subset of pre's,
    AND the two bodies share at least one top-level declaration name.

    The pg_state.py / comfyui_krita.py failure mode this catches: an AI
    "full" print that truncates one or more regions with a placeholder
    (`... (docstring unchanged) ...`, `/* ... rest unchanged */`) drops
    the pre-existing helpers and docstrings.  Verbatim substring fails
    because the placeholder text isn't in base.  Token-subset succeeds
    because every identifier the fragment actually uses appears in
    base; only the placeholder is new, and it carries no meaningful
    tokens.

    False positives (the real body just happens to be a token-subset of
    pre by chance) are mitigated by the top-level-decl requirement:
    the real body must declare something that pre also declares.
    """
    real_tokens = _meaningful_tokens(real_body)
    pre_tokens  = _meaningful_tokens(pre_body)
    if not real_tokens or not real_tokens.issubset(pre_tokens):
        return False
    return bool(_top_level_decls(real_body) & _top_level_decls(pre_body))


def _covers_preexisting(real_body, pre_body, threshold=0.5):
    """True iff the real body declares at least `threshold` fraction of
    the preexisting body's top-level declarations.

    This is the second guard against "full" prints that are actually
    fragments.  Subset-demote (`_token_subset`) catches fragments that
    are literally contained in base.  Coverage-demote catches fragments
    that are *rewrites* of a subset — new imports, new helper functions,
    or renamed parameters make a fragment not-a-substring, but the
    fragment still declares only a small fraction of the file's
    top-level names.

    The 0.5 threshold:
      · pg_state.py:     1/5  = 0.20  → demote
      · comfyui_krita.py: 1/22 = 0.045 → demote
      · full-file rewrite: 22/22 = 1.0 → keep
      · "rewrite most of it": >= 11/22 → keep

    A body with zero top-level declarations (e.g. an HTML fragment,
    a JSON dump) returns False — it can't cover a file with any
    declarations.  A preexisting body with zero declarations returns
    True (nothing to preserve, so nothing to fail coverage against).
    """
    pre_decls = _top_level_decls(pre_body)
    if not pre_decls:
        return True
    real_decls = _top_level_decls(real_body)
    if not real_decls:
        return False
    overlap = len(pre_decls & real_decls)
    return overlap / len(pre_decls) >= threshold


def _has_code_signal(body: str) -> bool:
    """True iff `body` looks like code rather than prose.

    Permissive by design.  False positives lead to failed patches
    (caught by fail-closed), while false negatives lead to silent
    drops (the class of failure this policy exists to eliminate).
    """
    if not body:
        return False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(kw) for kw in _CODE_KEYWORDS):
            return True
        if _CODE_PUNCT_LINE_RE.search(stripped):
            return True
    return False


# ---------------------------------------------------------------------------
# Blockquote policy — the single source of truth
# ---------------------------------------------------------------------------
#
# A blockquote wraps the AI's reasoning (deeppeek's own formatter
# wraps thoughts in `> `; the GUI does the same).  By convention such
# content is speculative.  The one signal strong enough to override
# that convention is **exact prose attribution** — the prose
# immediately above the fence names exactly ONE target file
# (`attributed_by == "prose"`) AND that prose reads as an instruction
# to commit code, not as a description of where something lives.
#
# Every other attribution strength is rejected:
#
#   · `prose_nearest` — the prose named MULTIPLE files and the
#     pipeline guessed the closest.  Thinking code routinely
#     straddles several files; the guess is unreliable.  This is
#     the pg_inpaint.py counterexample: a prose_nearest blockquoted
#     block was applied AFTER a correct scoped-replace pair had
#     already fixed the file, nesting `buildContextMaskB64` inside
#     itself.  Balanced-brace clean, silently wrong.
#
#   · `body_header`, `anchor`, `subset` — incidental matches
#     against the thinking's working notes.  The AI is not
#     signalling "commit this"; it happens to mention a filename, or
#     an anchor line, or a snippet.
#
#   · `prose` where the prose merely NAMES the destination file
#     without instructing insertion.  This is the pg_comfy_client.py
#     counterexample: an orphan blockquoted `body: JSON.stringify`
#     fragment was applied because the prose mentioned
#     `pg_comfy_client.py`, but the prose was describing what the
#     file does, not asking for the fragment to be inserted.  It
#     landed in the signature and corrupted the destructured
#     parameter list.
#
# Do not inline the check at any call site.  Every consumer of the
# blockquote policy calls `_has_strong_blockquote_attribution`,
# which reads `_STRONG_BQ_ATTR` and `_prose_has_commit_language`.
# To make the policy more or less permissive, edit either one — the
# predicate and every gate that uses it pick up the change together.
# A prior version had an inline `_STRONG_BQ_ATTR` check in one gate
# (`orphan_patches`) and a predicate call in the other
# (`_is_real_patch`); a tightening of the predicate silently diverged
# from the inline check, producing two contradictory policies within
# the same function — that drift is the bug this comment exists to
# prevent.

_STRONG_BQ_ATTR = frozenset({"prose"})


# Prose verbs/phrases that mark an *instruction* ("insert this here")
# as opposed to a *description* ("this file contains X", "the patch
# modifies Y").  Only instructions are strong enough to override the
# blockquote-is-thinking convention.  The list is deliberately narrow:
# adding weak verbs (e.g. "here is", "this") makes the gate fire on
# descriptive prose.
_COMMIT_VERBS = (
    "add this", "add the following", "insert this",
    "insert the following", "replace", "change",
    "update", "use this", "apply this", "here is the new",
    "here's the new", "here is the updated",
    "here's the updated",
    "with:", "with the following",
)


def _prose_has_commit_language(prose: str) -> bool:
    """True iff the prose reads as an instruction to commit the
    following block, not as a description of where something lives.

    A prose line like "the patch modifies `runInpaint`" names a file
    but does not commit code; a prose line like "add this to
    `runInpaint`" does.  Blockquoted orphans only override the
    blockquote-is-thinking convention when the prose is an
    instruction.
    """
    lower = (prose or "").lower()
    return any(v in lower for v in _COMMIT_VERBS)


def _has_strong_blockquote_attribution(b: Block) -> bool:
    """True iff the block's attribution is strong enough to override
    the blockquote-is-thinking convention.

    Two conditions, both required:

      1. `attributed_by == "prose"` — the surrounding prose names
         EXACTLY ONE target file.  `prose_nearest` (the multi-file
         guess) is rejected; see the module-level blockquote policy
         comment for the counterexamples.

      2. The prose carries commit language — "add this", "replace",
         "update", "with:", etc.  A prose line that merely NAMES the
         destination file without instructing the model to insert
         code is not a commit signal.  This is the pg_comfy_client.py
         counterexample: an orphan blockquoted `body: JSON.stringify`
         fragment was applied because the prose mentioned the file,
         but the prose was describing what the file does, not asking
         for the fragment to be inserted.  It landed in the signature
         and corrupted the destructured parameter list.

    This predicate and `_STRONG_BQ_ATTR` are the SINGLE source of
    truth for the policy.  Every call site in `pass5_deduplicate` —
    the `orphan_patches` construction and `_is_real_patch` — must
    go through this predicate.  Callers must not inline the check.
    """
    return (
        b.attributed_by in _STRONG_BQ_ATTR
        and _prose_has_commit_language(b.context_before)
    )


# ---------------------------------------------------------------------------
# Cross-file presence — attribution sanity check
# ---------------------------------------------------------------------------

def _already_in_other_file(block: Block,
                           current_path: str,
                           records: dict,
                           blocks_by_idx: dict) -> Optional[str]:
    """Return the path of another record whose content (committed
    content, or chosen-full base if content is None) contains the
    block's body, or None.

    Two match modes:
      · Exact substring — the block's body is contiguous in the
        other file's content.
      · Fuzzy line match — at least 85% of the block's lines are
        present in the other file's content.

    EXTENDED (2026-10-28): also considers records that are not
    `preexisting_base`.  A blockquoted fragment duplicated verbatim
    by a conversation-authored record elsewhere is a duplicate
    regardless of whether the destination record came from a
    preexisting file or from a conversation full-print.  The
    motivating cases:

      · `pg_panel_scrubs.py` blockquoted `_clearImageAction`
        fragment, verbatim prefix of `pg_panel_bindings.py`'s
        pre-existing base (which was itself authored by an earlier
        conversation).

      · `pg_panel_bindings.py` blockquoted `inpaintEnabledEl`
        fragment, verbatim of `pg_panel_scrubs.py`'s
        conversation-authored content.

    Both were misattributed by the current attribution and would
    have been applied to the wrong file if not caught.  Neither
    was caught by the previous preexisting-only guard.

    When `rec.content` is None (pass 5 runs before pass 6), falls
    back to the record's chosen-full block's body.  For
    conversation-authored records this is the correct content
    reference — it is the block the record would be based on
    before patches.
    """
    from .patch_text import _content_present_in_base

    body = block.body.strip()
    if not body or len(body) < 20:
        return None

    for other_path, other_rec in records.items():
        if other_path == current_path:
            continue

        other_content = other_rec.content
        if other_content is None and other_rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(other_rec.last_full_idx)
            if base_block is not None:
                other_content = base_block.body

        if other_content is None:
            continue

        if body in other_content:
            return other_path
        if _content_present_in_base(other_content, body,
                                    threshold=0.85):
            return other_path
    return None


def _is_covered_by_headed(block: Block, path: str, all_blocks) -> bool:
    """True iff `block`'s body is already contained in a headed
    (non-blockquoted) block attributed to `path`.

    Used to drop blockquoted thinking fragments whose payload is
    duplicated by a headed patch on the same file.  The headed patch
    is the strong channel; the blockquoted fragment is redundancy
    that only produces warning noise if it fails to anchor.

    Deliberately strict: requires verbatim substring containment.
    A near-miss (reformatted, reworded) is NOT considered covered,
    because the two blocks might implement subtly different shapes
    and neither is strictly redundant.  Let the blockquoted one
    attempt to apply; if it fails, warnings.txt names it.
    """
    body = (block.body or "").strip()
    if len(body) < 10:
        return False
    for other in all_blocks:
        if other is block:
            continue
        if other.in_blockquote:
            continue
        if not other.header_path:
            continue
        if other.output_path != path:
            continue
        if body in (other.body or ""):
            return True
    return False


def _is_preexisting_target(rec: FileRecord,
                           blocks_by_idx: dict) -> bool:
    """True iff any of `rec`'s source blocks is a pre-existing
    baseline.

    `rec.preexisting_base` is not set until `pass5_deduplicate`'s
    per-record loop has run; the reclassification step needs the
    same information *before* that point.  Inspecting each source
    block's `classified_by` (set by `_load_preexisting_blocks`
    during pipeline input assembly) gives the answer at any time
    after pass 3.
    """
    for idx in rec.source_blocks:
        b = blocks_by_idx.get(idx)
        if b is not None and b.classified_by == "preexisting":
            return True
    return False


def pass5_deduplicate(records: dict,
                      blocks_by_idx: dict,
                      verbose: bool) -> None:
    # ---- embedded-fragment reclassification (2026-11-01b) ----
    #
    # A `full`-kind orphan attributed to a pre-existing `.py` file
    # whose fence language is JS/HTML/CSS is an embedded fragment,
    # not a full replacement.  Pass 2's classification fell back to
    # `_looks_like_full_file(body, "unknown_N")` (target extension
    # unknown at that point); the non-`.py` fallback returned `True`.
    # Once attribution resolves the target, the label is provably
    # wrong: a `.py` file cannot become a `js` body.
    #
    # See module docstring, EMBEDDED-FRAGMENT RECLASSIFICATION
    # section, for the motivating case (pg_panel_scrubs.py's
    # `_clearImageAction` fragment, block idx=72 in the 2026-10-28
    # run).
    for _b in blocks_by_idx.values():
        if not _b.is_orphan or _b.kind != "full" or not _b.attributed_to:
            continue
        _target = records.get(_b.attributed_to)
        if _target is None:
            continue
        if not _is_preexisting_target(_target, blocks_by_idx):
            continue
        _dest_ext = (_b.attributed_to.rsplit(".", 1)[-1].lower()
                     if "." in _b.attributed_to else "")
        _block_lang = (_b.fence_lang or "").lower()
        if _dest_ext == "py" and _block_lang in _EMBEDDED_BLOCK_LANGS:
            if verbose:
                print(f"  [pass 5] reclassifying full orphan "
                      f"idx={_b.idx} -> partial "
                      f"(embedded {_block_lang} in .py)",
                      file=sys.stderr)
            _b.kind = "partial"
            _b.heuristic_kind = "partial"

    # ---- detect replace / scoped pairs first ----
    detect_all(list(blocks_by_idx.values()), blocks_by_idx)
    global_pairs = build_pairs(blocks_by_idx)

    # ---- global block ownership ----
    # Each block idx belongs to at most one record.  Precedence:
    #   (1) explicit pair (old_idx, new_idx)
    #   (2) source_blocks of a record
    #   (3) orphan attribution
    # Prevents the same block from being applied as a patch in two
    # different records (which would silently duplicate the edit).
    block_owner = {}
    for anchor, pair in global_pairs.items():
        if pair["old_idx"] == -1:
            b = blocks_by_idx[anchor]
            if b.output_path:
                block_owner[anchor] = b.output_path
        else:
            b_old = blocks_by_idx.get(pair["old_idx"])
            b_new = blocks_by_idx.get(pair["new_idx"])
            if b_old and b_old.output_path:
                block_owner[pair["old_idx"]] = b_old.output_path
            if b_new and b_new.output_path:
                block_owner[pair["new_idx"]] = b_new.output_path
    for _path, _rec in records.items():
        for _idx in _rec.source_blocks:
            if _idx not in block_owner:
                block_owner[_idx] = _path
    for _b in blocks_by_idx.values():
        if not _b.is_orphan or not _b.attributed_to:
            continue
        if _b.idx not in block_owner:
            block_owner[_b.idx] = _b.attributed_to
        elif block_owner[_b.idx] != _b.attributed_to and verbose:
            print(f"  [pass 5] block idx={_b.idx} claimed by "
                  f"{block_owner[_b.idx]!r} (source), also "
                  f"attributed to {_b.attributed_to!r}; ignoring "
                  f"the attribution", file=sys.stderr)

    for path, rec in records.items():
        all_fulls = [i for i in rec.source_blocks
                     if blocks_by_idx[i].kind == "full"]
        real_fulls = [i for i in all_fulls
                      if blocks_by_idx[i].classified_by != "preexisting"]
        pre_fulls = [i for i in all_fulls
                     if blocks_by_idx[i].classified_by == "preexisting"]

        # Subset-demote OR coverage-demote: a conversation "full"
        # that is a strict substring of a preexisting full, or that
        # declares too few of the preexisting file's top-level
        # declarations, is not a replacement of the whole file — it
        # is a partial extraction or a scoped rewrite.  Demote it
        # to a patch and keep the preexisting as the base.
        #
        # A real block that passes BOTH checks (not a subset AND
        # covers >=50% of pre's declarations) is a genuine full
        # replacement; the preexisting base is discarded in favour
        # of the real body.
        #
        # See module docstring: SUBSET-DEMOTE POLICY and
        # COVERAGE-DEMOTE POLICY.
        if real_fulls and pre_fulls:
            pre_bodies = {i: blocks_by_idx[i].body for i in pre_fulls}
            all_reals_cover = True
            for i in real_fulls:
                real_body = (blocks_by_idx[i].body or "").strip()
                if not real_body:
                    all_reals_cover = False
                    break
                is_subset = any(
                    real_body in pb or _token_subset(real_body, pb)
                    for pb in pre_bodies.values()
                )
                covers_pre = any(
                    _covers_preexisting(real_body, pb)
                    for pb in pre_bodies.values()
                )
                if is_subset or not covers_pre:
                    all_reals_cover = False
                    break
            if all_reals_cover:
                fulls = real_fulls
                rec.preexisting_base = False
                rec._demoted_reals = set()
            else:
                fulls = pre_fulls
                rec.preexisting_base = True
                rec._demoted_reals = set(real_fulls)
                if verbose:
                    for i in real_fulls:
                        real_body = (blocks_by_idx[i].body or "").strip()
                        for pb in pre_bodies.values():
                            if (real_body in pb
                                    or _token_subset(real_body, pb)):
                                print(f"  [pass 5] {path}: demoting "
                                      f"idx={i} (subset of preexisting)",
                                      file=sys.stderr)
                                break
                            if not _covers_preexisting(real_body, pb):
                                pre_n = len(_top_level_decls(pb))
                                real_n = len(_top_level_decls(real_body))
                                print(f"  [pass 5] {path}: demoting "
                                      f"idx={i} (declares {real_n}/"
                                      f"{pre_n} of preexisting "
                                      f"declarations)", file=sys.stderr)
                                break
        elif real_fulls:
            fulls = real_fulls
            rec.preexisting_base = False
            rec._demoted_reals = set()
        elif pre_fulls:
            fulls = pre_fulls
            rec.preexisting_base = True
            rec._demoted_reals = set()
        else:
            fulls = []
            rec.preexisting_base = False
            rec._demoted_reals = set()

        # ---- pick chosen_full_idx BEFORE building pairs ----
        # The pair validation (see next block) needs the base body
        # to check scope existence and self-reference.  Picking the
        # winner first lets the check read blocks_by_idx[chosen].
        base_body = ""
        chosen_idx = None
        if fulls:
            rec.all_full_idxs = fulls[:]
            if len(fulls) == 1:
                rec.chosen_full_idx = fulls[0]
                rec.dedup_reason = (
                    "preexisting_base" if rec.preexisting_base
                    else "single_full")
            else:
                chosen, reason = _dedup_pick(path, fulls, blocks_by_idx)
                rec.chosen_full_idx = chosen
                rec.dedup_reason = reason
                if verbose:
                    print(f"  [pass 5] {path}: chose idx={chosen} "
                          f"({reason}); candidates={fulls}",
                          file=sys.stderr)
            rec.last_full_idx = rec.chosen_full_idx
            chosen_idx = rec.chosen_full_idx
            base_body = blocks_by_idx[chosen_idx].body

        # ---- pair-assigned blocks (with validation) ----
        #
        # A scoped pair is only valid when:
        #   · it does NOT target the chosen full block itself
        #     (self-nesting: the block IS the base), AND
        #   · the scope name exists in the base (this is a
        #     REPLACE, not an INSERT of a new scope).
        #
        # When either check fails, the pair is rejected and the
        # block falls through to the trailing-patch path, where the
        # insert heuristics have a chance.  See module docstring.
        pair_idxs_for_this_file = set()
        for anchor, pair in global_pairs.items():
            if pair["old_idx"] == -1:
                b = blocks_by_idx[anchor]
                if b.output_path != path:
                    continue
                if anchor == chosen_idx:
                    if verbose:
                        print(f"  [pass 5] {path}: skipping scoped pair "
                              f"idx={anchor} (targets chosen full)",
                              file=sys.stderr)
                    continue
                if base_body and not _scope_in_base(
                        base_body, pair["scope_name"],
                        getattr(b, "scope_hint", "") or ""):
                    if verbose:
                        print(f"  [pass 5] {path}: scoped pair idx="
                              f"{anchor} scope={pair['scope_name']!r} "
                              f"not in base; treating as insert",
                              file=sys.stderr)
                    continue
                pair_idxs_for_this_file.add(anchor)
                rec.pairs.append({
                    "old_idx": -1,
                    "new_idx": anchor,
                    "scope_hint": "scoped",
                    "scope_name": pair["scope_name"],
                    "new_body": pair["new_body"],
                })
            else:
                b_old = blocks_by_idx[pair["old_idx"]]
                b_new = blocks_by_idx[pair["new_idx"]]
                if b_old.output_path != path and \
                   b_new.output_path != path:
                    continue
                pair_idxs_for_this_file.add(pair["old_idx"])
                pair_idxs_for_this_file.add(pair["new_idx"])
                rec.pairs.append({
                    "old_idx": pair["old_idx"],
                    "new_idx": pair["new_idx"],
                    "scope_hint": "replace",
                    "old_body": pair["old_body"],
                    "new_body": pair["new_body"],
                })

        partials = [i for i in rec.source_blocks
                    if blocks_by_idx[i].kind == "partial"
                    and i not in pair_idxs_for_this_file
                    and block_owner.get(i) == path]
        # Demoted reals from the subset/coverage check above also
        # need to run as patches — they are scoped replacements of
        # functions against a preexisting base.
        for _i in getattr(rec, "_demoted_reals", set()):
            if _i not in partials:
                partials.append(_i)
        # Orphan blocks inside a blockquote come from AI thinking.
        # Apply them only when attribution is strong enough to
        # override the blockquote-is-thinking convention — see
        # `_has_strong_blockquote_attribution`'s docstring and the
        # module-level blockquote policy comment.  Both this gate
        # and `_is_real_patch` below must share the SAME predicate;
        # inlining the check here was the source of the two-gate
        # drift bug this comment guards against.
        #
        # Cross-file presence: an orphan blockquoted block whose
        # body is already verbatim in a DIFFERENT pre-existing
        # record is a reference, not a new insertion.  Prose
        # attribution that happens to name a filename does not
        # override this — the AI's own source already proves where
        # the fragment lives.  pg_inpaint.py failure case.
        #
        # Covered-by-headed: an orphan blockquoted block whose body
        # is a verbatim substring of a headed patch on the same
        # file is redundancy.  The headed patch is the strong
        # channel; the blockquoted fragment only produces warning
        # noise if it fails to anchor.  comfyui.py blockquoted
        # `--output_rotation` arg fragment vs headed block 655.
        #
        # FIX (2026-11-01b): `kind != "full"` is gone from the filter
        # below.  A `full` label on an attributed orphan is unreliable
        # (pass 2's classification fell back to a non-`.py` heuristic
        # that returns True for any multi-line body when the target's
        # extension is unknown).  Once attribution resolves the target,
        # the label is meaningless — the block is a fragment to be
        # inserted or spliced, not a replacement for the whole file.
        # `_is_real_patch` still applies its code-signal and blockquote
        # filters, so this does not admit prose.  The reclassification
        # loop at the top of this function handles the language-
        # mismatch case cleanly; this allowance covers everything else
        # (e.g. an HTML body attributed to a `.html` pre-existing
        # file — the reclassification skips it because there is no
        # language mismatch, but the block still must be a patch).
        orphan_patches = []
        for _b in blocks_by_idx.values():
            if not (_b.is_orphan and _b.attributed_to == path
                    and _b.idx not in pair_idxs_for_this_file
                    and block_owner.get(_b.idx) == path
                    and (not _b.in_blockquote
                         or _has_strong_blockquote_attribution(_b))):
                continue
            if _b.in_blockquote:
                if _is_covered_by_headed(
                        _b, path, blocks_by_idx.values()):
                    if verbose:
                        print(f"  [pass 5] {path}: skipping orphan "
                              f"idx={_b.idx} — covered by a headed "
                              f"block", file=sys.stderr)
                    continue
                other = _already_in_other_file(
                    _b, path, records, blocks_by_idx)
                if other is not None:
                    if verbose:
                        print(f"  [pass 5] {path}: skipping orphan "
                              f"idx={_b.idx} — already in {other!r}",
                              file=sys.stderr)
                    continue
            orphan_patches.append(_b.idx)
        partials = sorted(partials + orphan_patches)

        if verbose and rec.pairs:
            print(f"  [pass 5] {path}: {len(rec.pairs)} pair(s)",
                  file=sys.stderr)

        if not fulls:
            if rec.pairs:
                rec.state = "PARTIAL_ONLY"
                if verbose:
                    print(f"  [pass 5] {path}: pairs but no full print; "
                          f"pairs cannot apply", file=sys.stderr)
            else:
                rec.state = "PARTIAL_ONLY" if partials else "MISSING"
            continue

        # chosen_full_idx already set above.

        def _is_real_patch(p_idx: int) -> bool:
            if p_idx in pair_idxs_for_this_file:
                return False
            if p_idx in getattr(rec, "_demoted_reals", set()):
                return True
            b = blocks_by_idx[p_idx]
            if b.override_reason == "recovered-meta listing":
                return False
            if b.override_reason and (
                "diff-context anchor" in b.override_reason
                or "reprint-context anchor" in b.override_reason
            ):
                return True

            # Blockquoted content is thought-process unless
            # `_has_strong_blockquote_attribution` says otherwise.
            # The rationale is in that predicate's docstring and the
            # module-level blockquote policy comment.  This gate and
            # the `orphan_patches` construction above must share the
            # SAME predicate — see the policy comment for the
            # two-gate drift bug this contract prevents.
            if b.in_blockquote and not _has_strong_blockquote_attribution(b):
                return False

            body = b.body.strip()
            if not body:
                return False

            # Structured patch types — always accepted.
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

            # Policy (2026-11-01c): any remaining attributed partial
            # with a code signal is a patch candidate.  Silent drops
            # are strictly worse than fail-closed rejections.
            return _has_code_signal(body)

        # Trailing filter.
        #
        # Historically `p > chosen_idx`: a partial appearing BEFORE
        # the chosen full print is treated as a superseded draft.
        # That rule is correct for headed blocks (the AI printed a
        # draft, then re-printed the full file).
        #
        # It is NOT correct for orphans.  Orphans have no header and
        # no print-order relationship to the base.  Their position in
        # the index list is an artefact of `_load_preexisting_blocks`
        # prepending the pre-existing tree ahead of the conversation
        # blocks — the orphan's idx is high in the usual case, but
        # that is coincidental, not semantic.  Including every
        # attributed orphan regardless of position is the correct
        # filter; `_is_real_patch` already rejects thinking fragments
        # and prose.
        trailing = [
            p for p in partials
            if _is_real_patch(p) and (
                p > chosen_idx
                or blocks_by_idx[p].is_orphan
            )
        ]

        if trailing:
            rec.state = "FULL_PLUS_PATCH"
            rec.patches = [
                {
                    "idx": p,
                    "body": blocks_by_idx[p].body,
                    "context_before": blocks_by_idx[p].context_before,
                    "fence_lang": blocks_by_idx[p].fence_lang,
                }
                for p in trailing
            ]
        elif len(fulls) == 1:
            rec.state = "SINGLE_FULL"
        else:
            rec.state = "MULTI_FULL"

        if rec.pairs and rec.state == "SINGLE_FULL":
            rec.state = "FULL_PLUS_PATCH"


def _dedup_pick(path: str,
                fulls: list,
                blocks_by_idx: dict) -> tuple:
    if len(fulls) == 1:
        return fulls[0], "single"

    bodies = [blocks_by_idx[i].body for i in fulls]
    if len(set(bodies)) == 1:
        return fulls[-1], "identical"

    for i in fulls:
        bi = blocks_by_idx[i]
        if all(
            j == i or
            (len(bi.body) > len(blocks_by_idx[j].body) * 1.3
             and blocks_by_idx[j].body.strip() in bi.body)
            for j in fulls
        ):
            return i, "superset"

    _SIM_THRESHOLD = 0.75
    _SIM_MIN_LINES = 20

    body_lines = {
        i: {ln.strip() for ln in blocks_by_idx[i].body.splitlines()
            if ln.strip()}
        for i in fulls
    }

    qualifies = {}
    for j in fulls:
        bj = body_lines[j]
        if len(bj) < _SIM_MIN_LINES:
            continue
        max_sim = 0.0
        for i in fulls:
            if i >= j:
                continue
            ai = body_lines[i]
            if len(ai) < _SIM_MIN_LINES:
                continue
            union = ai | bj
            if not union:
                continue
            sim = len(ai & bj) / len(union)
            if sim > max_sim:
                max_sim = sim
        if max_sim >= _SIM_THRESHOLD:
            qualifies[j] = max_sim

    if qualifies:
        newest = max(qualifies.keys())
        return newest, f"revision (sim {qualifies[newest]:.2f})"

    if path.endswith(".py"):
        import ast
        valid = []
        for i in fulls:
            try:
                ast.parse(blocks_by_idx[i].body)
                valid.append(i)
            except SyntaxError:
                pass
        if len(valid) == 1:
            return valid[0], "syntax_preferred"
        if 1 < len(valid) < len(fulls):
            fulls = valid

    def mod_score(b):
        return MODIFIER_RANK.get((b.modifier or "").lower().strip(), 0)

    scored = [(mod_score(blocks_by_idx[i]), i) for i in fulls]
    top = max(s for s, _ in scored)
    bottom = min(s for s, _ in scored)
    if top > bottom:
        winners = [i for s, i in scored if s == top]
        if len(winners) == 1:
            return winners[0], "modifier"

    flagged = []
    for i in fulls:
        ctx = blocks_by_idx[i].context_before.lower()
        if any(p in ctx for p in FIX_PROSE_PHRASES):
            flagged.append(i)
    if len(flagged) == 1:
        return flagged[0], "prose_fix"

    if path.endswith(".py"):
        import ast
        valid = []
        for i in fulls:
            try:
                ast.parse(blocks_by_idx[i].body)
                valid.append(i)
            except SyntaxError:
                pass
        if len(valid) == 1:
            return valid[0], "syntax"

    return fulls[-1], "last_full"
