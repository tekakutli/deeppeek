"""
recover.dedup — Pass 5 (pick one full-print winner per file) plus
pair detection.

Pair detection (new in 2026-11-01) runs first.  Any block that is
half of a replace pair or the single half of a scoped replacement
is *removed* from the normal trailing-patch path — it becomes a
structured edit that `apply_patches` handles separately.

Without this, a replace pair was being mangled: the OLD block was
seen as a delete (matched the base exactly), the NEW block was
dropped as an unclassifiable snippet (no anchor in base).  Result:
half the transformation applied.
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


def pass5_deduplicate(records: dict[str, FileRecord],
                      blocks_by_idx: dict[int, Block],
                      verbose: bool) -> None:
    # ---- new: detect replace / scoped pairs first ----
    detect_all(list(blocks_by_idx.values()), blocks_by_idx)
    global_pairs = build_pairs(blocks_by_idx)

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

        # ---- new: pair-assigned blocks are excluded from patches ----
        pair_idxs_for_this_file = set()
        for anchor, pair in global_pairs.items():
            if pair["old_idx"] == -1:
                # Scoped: block anchor is both old and new
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
                    and i not in pair_idxs_for_this_file]
        orphan_patches = [
            b.idx for b in blocks_by_idx.values()
            if b.is_orphan and b.attributed_to == path
            and b.kind != "full"
            and b.idx not in pair_idxs_for_this_file
        ]
        partials = sorted(partials + orphan_patches)

        if verbose and rec.pairs:
            print(f"  [pass 5] {path}: {len(rec.pairs)} pair(s)",
                  file=sys.stderr)

        if not fulls:
            # Pairs require a base to operate on.  If there's no full
            # print, the pair cannot apply.
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

            if _is_deletion_instruction(body, b.context_before):
                return True

            if b.fence_lang.lower() == "diff":
                return True

            if _looks_like_diff_body(body):
                return True

            first_line = body.split("\n", 1)[0].strip()
            if re.match(
                r'^export\s+(?:const|let|var)\s+\w+\s*=\s*\{',
                first_line,
            ):
                return True
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

        # A file with only pairs (no trailing patches, one full
        # print) still needs FULL_PLUS_PATCH so apply_patches runs.
        if rec.pairs and rec.state == "SINGLE_FULL":
            rec.state = "FULL_PLUS_PATCH"


def _dedup_pick(path: str,
                fulls: list[int],
                blocks_by_idx: dict[int, Block]) -> tuple[int, str]:
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
