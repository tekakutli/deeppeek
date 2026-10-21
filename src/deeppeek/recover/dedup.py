"""
recover.dedup — Pass 5 (pick one full-print winner per file).

DIFFICULT.  Owns:

    FIX [B]   Syntax-preference step between "revision" (2.5) and
              "modifier" (3).  For .py files, restrict the candidate
              set to ast.parse-valid prints before running the modifier
              signal.  A "corrected" reprint that fails ast.parse must
              never beat an older valid print.  This recovers
              pg_base.py — the fonts-fixed reprint has an unterminated
              docstring that swallowed the body.

    FIX [C]   Filter spurious trailing "patches".  A partial block
              after the chosen full print becomes a real patch ONLY
              if it satisfies one of:

                · override_reason is a diff- or reprint-context anchor
                  (i.e., the prose immediately before the block says
                  "change inside …" or "reprint in place …"), or

                · the body's first line is a structural diff target —
                  an object-literal re-declaration (`export const X =
                  {...}` / `export let X = {...}` / `export var X =
                  {...}`), which the deterministic patcher handles
                  directly, or

                · the body is a unified diff hunk (a `@@ -a,b +c,d @@`
                  header), which the LLM patcher handles.

              Everything else is dropped.  The prior rule ("small
              body AND shares a 20+ char line with the base") let
              any prose snippet that quoted a line of the base
              through; the LLM-off patcher then failed on all of
              them and every failure appended a warning line, so a
              single file could accumulate dozens of phantom patch
              failures that dragged trust to LOW and (under
              --min-trust HIGH) silently killed files that had a
              perfectly good full print.
"""

import re
import sys
from typing import Optional

from .core import Block, FileRecord, FIX_PROSE_PHRASES, MODIFIER_RANK


def pass5_deduplicate(records: dict[str, FileRecord],
                      blocks_by_idx: dict[int, Block],
                      verbose: bool) -> None:
    for path, rec in records.items():
        fulls = [i for i in rec.source_blocks
                 if blocks_by_idx[i].kind == "full"]
        partials = [i for i in rec.source_blocks
                    if blocks_by_idx[i].kind == "partial"]
        orphan_patches = [
            b.idx for b in blocks_by_idx.values()
            if b.is_orphan and b.attributed_to == path
        ]
        partials = sorted(partials + orphan_patches)

        if not fulls:
            rec.state = "PARTIAL_ONLY" if partials else "MISSING"
            continue

        rec.all_full_idxs = fulls[:]

        if len(fulls) == 1:
            rec.chosen_full_idx = fulls[0]
            rec.dedup_reason = "single_full"
        else:
            chosen, reason = _dedup_pick(path, fulls, blocks_by_idx)
            rec.chosen_full_idx = chosen
            rec.dedup_reason = reason
            if verbose:
                print(f"  [pass 5] {path}: chose idx={chosen} "
                      f"({reason}); candidates={fulls}",
                      file=sys.stderr)

        rec.last_full_idx = rec.chosen_full_idx

        # FIX [C]: real patches only.
        chosen_idx = rec.chosen_full_idx

        def _is_real_patch(p_idx: int) -> bool:
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
            first_line = body.split("\n", 1)[0].strip()
            # Object-literal re-declaration — the deterministic
            # patcher's target shape.
            if re.match(
                r'^export\s+(?:const|let|var)\s+\w+\s*=\s*\{',
                first_line,
            ):
                return True
            # Unified diff hunk — the LLM patcher's target shape.
            if body.startswith("@@ ") or "\n@@ " in body:
                return True
            return False

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


def _dedup_pick(path: str,
                fulls: list[int],
                blocks_by_idx: dict[int, Block]) -> tuple[int, str]:
    if len(fulls) == 1:
        return fulls[0], "single"

    # 1. All identical
    bodies = [blocks_by_idx[i].body for i in fulls]
    if len(set(bodies)) == 1:
        return fulls[-1], "identical"

    # 2. One strict superset of the rest
    for i in fulls:
        bi = blocks_by_idx[i]
        if all(
            j == i or
            (len(bi.body) > len(blocks_by_idx[j].body) * 1.3
             and blocks_by_idx[j].body.strip() in bi.body)
            for j in fulls
        ):
            return i, "superset"

    # 2.5. High-similarity revision
    _SIM_THRESHOLD = 0.75
    _SIM_MIN_LINES = 20

    body_lines = {
        i: {ln.strip() for ln in blocks_by_idx[i].body.splitlines()
            if ln.strip()}
        for i in fulls
    }

    qualifies: dict[int, float] = {}
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

    # FIX [B]: syntax preference — restrict to ast.parse-valid candidates
    # before running the modifier signal.  A "corrected" reprint that
    # fails ast.parse should never beat an older valid print.
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

    # 3. Modifier priority
    def mod_score(b):
        return MODIFIER_RANK.get((b.modifier or "").lower().strip(), 0)

    scored = [(mod_score(blocks_by_idx[i]), i) for i in fulls]
    top = max(s for s, _ in scored)
    bottom = min(s for s, _ in scored)
    if top > bottom:
        winners = [i for s, i in scored if s == top]
        if len(winners) == 1:
            return winners[0], "modifier"

    # 4. Prose-fix signal
    flagged = []
    for i in fulls:
        ctx = blocks_by_idx[i].context_before.lower()
        if any(p in ctx for p in FIX_PROSE_PHRASES):
            flagged.append(i)
    if len(flagged) == 1:
        return flagged[0], "prose_fix"

    # 5. Syntax check
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

    # 6. Last-resort tiebreaker
    return fulls[-1], "last_full"
