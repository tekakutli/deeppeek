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
    """Return the path of another record whose base contains the
    block's body, or None.

    Two match modes:

      · Exact substring — the block's body is contiguous in the
        other file's base.  This is the original behaviour.

      · Fuzzy line match — at least 85% of the block's lines are
        present in the other file's base (exact or token-
        equivalent).  This catches inline fragments of longer
        functions: block 86 in the inpaint conversation is
        `_clearImageAction`'s prefix (six lines, closed by a bare
        `}`), while the base it references in `pg_panel_bindings.py`
        is those same six lines followed by
        `import("./pg_dispatch.js").then(...)` and a different `}`
        line.  Exact substring fails because the closing brace is
        not contiguous; per-line fuzzy succeeds.

    Threshold 0.85 is deliberately conservative.  A block whose
    content is fully shadowed by another file's base is very
    unlikely to be a legitimate edit of the current file — the
    prose attribution guessed wrong.  Failing closed here costs
    one manual copy; a false negative (applying the block anyway)
    corrupts the wrong file, whose content then diverges from the
    intended shape.

    Only checks *preexisting* records: a block that IS the base of
    the current file is already handled by the chosen-full filter
    in `pass5_deduplicate`.
    """
    from .patch_text import _content_present_in_base

    body = block.body.strip()
    if not body or len(body) < 20:
        return None
    for other_path, other_rec in records.items():
        if other_path == current_path:
            continue
        if not other_rec.preexisting_base:
            continue
        if other_rec.last_full_idx is None:
            continue
        other_base = blocks_by_idx.get(other_rec.last_full_idx)
        if other_base is None:
            continue
        if body in other_base.body:
            return other_path
        if _content_present_in_base(other_base.body, body,
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


def pass5_deduplicate(records: dict,
                      blocks_by_idx: dict,
                      verbose: bool) -> None:
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

        # Subset-demote: a conversation "full" that is a strict
        # substring of a preexisting full is not a replacement of
        # the whole file — it is a partial extraction.  Demote it
        # to a patch and keep the preexisting as the base.
        if real_fulls and pre_fulls:
            pre_bodies = {i: blocks_by_idx[i].body for i in pre_fulls}
            all_subset = True
            for i in real_fulls:
                real_body = (blocks_by_idx[i].body or "").strip()
                if not real_body:
                    all_subset = False
                    break
                is_subset = False
                for pb in pre_bodies.values():
                    if real_body in pb:
                        is_subset = True
                        break
                    if _token_subset(real_body, pb):
                        is_subset = True
                        break
                if not is_subset:
                    all_subset = False
                    break
            if all_subset:
                fulls = pre_fulls
                rec.preexisting_base = True
                rec._demoted_reals = set(real_fulls)
            else:
                fulls = real_fulls
                rec.preexisting_base = False
                rec._demoted_reals = set()
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
        # Demoted reals from the subset check above also need to run
        # as patches — they are scoped replacements of functions
        # against a preexisting base.
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
        orphan_patches = []
        for _b in blocks_by_idx.values():
            if not (_b.is_orphan and _b.attributed_to == path
                    and _b.kind != "full"
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

        trailing = [p for p in partials
                    if p > chosen_idx and _is_real_patch(p)]

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
