"""
recover.dedup — Pass 5 (pick one full-print winner per file) plus
pair detection.

Pair detection runs first.  Any block that is half of a replace pair
or a scoped replace is *removed* from the normal trailing-patch path
— it becomes a structured edit that `apply_patches` handles
separately.

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

        if real_fulls:
            fulls = real_fulls
            rec.preexisting_base = False
        elif pre_fulls:
            fulls = pre_fulls
            rec.preexisting_base = True
        else:
            fulls = []
            rec.preexisting_base = False

        # ---- pair-assigned blocks are excluded from patches ----
        pair_idxs_for_this_file = set()
        for anchor, pair in global_pairs.items():
            if pair["old_idx"] == -1:
                b = blocks_by_idx[anchor]
                if b.output_path == path:
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
        # Orphan blocks inside a blockquote come from AI
        # thinking.  Apply them only when prose attribution (the
        # AI naming the file in prose) succeeded -- that is the
        # strongest signal that the AI intended to commit the
        # code.  Weaker attributions (body_header, anchor,
        # subset) on blockquoted blocks are almost always
        # incidental matches against the thinking's working
        # notes: the pipeline previously applied them, they
        # failed all insertion heuristics, and generated two
        # warnings per block for no benefit.
        _STRONG_BQ_ATTR = frozenset({"prose", "prose_nearest"})
        orphan_patches = [
            b.idx for b in blocks_by_idx.values()
            if b.is_orphan and b.attributed_to == path
            and b.kind != "full"
            and b.idx not in pair_idxs_for_this_file
            and block_owner.get(b.idx) == path
            and not (b.in_blockquote
                     and b.attributed_by not in _STRONG_BQ_ATTR)
        ]
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

        rec.all_full_idxs = fulls[:]

        if len(fulls) == 1:
            rec.chosen_full_idx = fulls[0]
            rec.dedup_reason = (
                "preexisting_base" if rec.preexisting_base else "single_full"
            )
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

        def _is_real_patch(p_idx: int) -> bool:
            if p_idx in pair_idxs_for_this_file:
                return False
            b = blocks_by_idx[p_idx]
            if b.override_reason == "recovered-meta listing":
                return False
            if b.override_reason and (
                "diff-context anchor" in b.override_reason
                or "reprint-context anchor" in b.override_reason
            ):
                return True
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
