"""
recover.resolve — path resolution between the conversation and the
pre-existing tree.

Runs after classify (needs `kind` for content scoring) and before
grouping.  Assigns `Block.output_path` to every non-orphan block:

    · Preexisting baselines (`classified_by == "preexisting"`) already
      carry their real path in `canonical_path`; the resolver copies
      it into `output_path` verbatim.

    · Conversation blocks with `--apply-to` set go through resolution
      against the pre-existing file index:

          1. Exact match: header_path == a pre-existing full path
          2. Unique strict suffix: e.g. "recover/pipeline.py" matches
             "src/deeppeek/recover/pipeline.py" if that suffix is
             unique AND longer than the header's basename (so a bare
             "pipeline.py" doesn't trigger this branch — that case
             falls to basename matching).
          3. Unique basename: exactly one pre-existing file shares the
             header's basename.
          4. Ambiguous basename: score each candidate by content
             similarity + context hints; commit only if the best score
             is ≥20 and exceeds the runner-up by ≥10.
          5. No match: fall back to the literal header_path (so a
             stray "recover/pipeline.py" writes to
             DIR/recover/pipeline.py, visible at the top level rather
             than silently merged into the wrong subdirectory).  The
             unresolved block is reported in the manifest.

    · Conversation blocks without `--apply-to` get `output_path =
      canonical_path` (basename), so standard recover stays flat.

    · Orphans (no header) are skipped here.  Pass 4 attributes them to
      a record, and their attachment key is that record's output_path,
      so they land in the right place without a separate resolution
      step.

WHY A SEPARATE MODULE
=====================
The resolver is the only code that has to reason about *both* the
literal conversation text and the actual filesystem, and the scoring
function is the only one with any heuristics that resemble "AI
judgement".  Keeping it out of classify / dedup / write lets each of
those stay purely structural.
"""
import os
import re
import sys
from typing import Optional

from .core import Block, _basename


_MIN_FULL_SUFFIX = 2  # "/" + header must appear in full_path


def resolve_paths(blocks: list[Block],
                  apply_to: Optional["Path"],
                  verbose: bool = False) -> list[dict]:
    """Assign Block.output_path on every block in place.

    Returns a list of `{idx, header_path, resolved_to, reason}` records
    for the manifest — one per resolved block, useful for auditing
    which decisions were made.
    """
    resolutions: list[dict] = []

    if apply_to is None:
        # Standard recover: everything is flat; identity == basename.
        for b in blocks:
            if b.output_path is not None:
                continue
            b.output_path = b.canonical_path or f"unknown_{b.idx}"
        return resolutions

    # ---- build pre-existing index ----
    preexisting_by_full: dict[str, Block] = {}
    preexisting_by_basename: dict[str, list[str]] = {}
    for b in blocks:
        if b.classified_by != "preexisting":
            continue
        full = b.canonical_path
        if not full:
            continue
        preexisting_by_full[full] = b
        base = os.path.basename(full)
        preexisting_by_basename.setdefault(base, []).append(full)

    # ---- resolve each block ----
    for b in blocks:
        if b.classified_by == "preexisting":
            b.output_path = b.canonical_path
            continue

        if b.is_orphan:
            continue

        hp = b.header_path
        if not hp:
            b.output_path = b.canonical_path or f"unknown_{b.idx}"
            continue

        resolved, reason = _resolve_one(b, preexisting_by_full,
                                        preexisting_by_basename, verbose)
        if resolved is not None:
            b.output_path = resolved
            resolutions.append({
                "idx": b.idx,
                "header_path": hp,
                "resolved_to": resolved,
                "reason": reason,
            })
        else:
            b.output_path = hp
            resolutions.append({
                "idx": b.idx,
                "header_path": hp,
                "resolved_to": hp,
                "reason": "unresolved (fallback: literal header_path)",
            })
            if verbose:
                print(f"  [resolve] idx={b.idx} {hp!r} unresolved, "
                      f"writing to {hp!r} verbatim", file=sys.stderr)

    return resolutions


def _resolve_one(block: Block,
                 preexisting_by_full: dict[str, Block],
                 preexisting_by_basename: dict[str, list[str]],
                 verbose: bool) -> tuple[Optional[str], str]:
    hp = block.header_path or ""
    if not hp:
        return None, "no header_path"

    # 1. Exact
    if hp in preexisting_by_full:
        if verbose:
            print(f"  [resolve] idx={block.idx} {hp!r} → {hp} (exact)",
                  file=sys.stderr)
        return hp, "exact"

    # 2. Strict suffix (header longer than basename)
    hp_base = os.path.basename(hp)
    if hp != hp_base:
        suffix_matches = [
            full for full in preexisting_by_full
            if full.endswith("/" + hp)
        ]
        if len(suffix_matches) == 1:
            if verbose:
                print(f"  [resolve] idx={block.idx} {hp!r} → "
                      f"{suffix_matches[0]} (suffix)", file=sys.stderr)
            return suffix_matches[0], "suffix"
        if len(suffix_matches) > 1:
            return _discriminate(block, suffix_matches,
                                 preexisting_by_full, verbose,
                                 "suffix-ambiguous")

    # 3. Basename
    candidates = preexisting_by_basename.get(hp_base, [])
    if len(candidates) == 1:
        if verbose:
            print(f"  [resolve] idx={block.idx} {hp!r} → "
                  f"{candidates[0]} (unique basename)", file=sys.stderr)
        return candidates[0], "basename"
    if len(candidates) > 1:
        return _discriminate(block, candidates, preexisting_by_full,
                             verbose, "basename-ambiguous")

    return None, "no match"


def _discriminate(block: Block, candidates: list[str],
                  preexisting_by_full: dict[str, Block],
                  verbose: bool,
                  reason_tag: str) -> tuple[Optional[str], str]:
    scores = [
        (full, _score(block, full, preexisting_by_full[full]))
        for full in candidates
    ]
    scores.sort(key=lambda x: x[1], reverse=True)

    if len(scores) == 1:
        return scores[0][0], reason_tag

    (best_path, best_score), (second_path, second_score) = scores[0], scores[1]
    absolute_gap = best_score - second_score

    if best_score >= 20.0 and absolute_gap >= 10.0:
        if verbose:
            print(f"  [resolve] idx={block.idx} {block.header_path!r} "
                  f"{reason_tag}: chose {best_path} "
                  f"(score {best_score:.1f} vs {second_score:.1f})",
                  file=sys.stderr)
        return best_path, f"{reason_tag} (gap {absolute_gap:.1f})"

    if verbose:
        print(f"  [resolve] idx={block.idx} {block.header_path!r} "
              f"{reason_tag}: no confident choice among "
              f"{[(p, round(s, 1)) for p, s in scores]}",
              file=sys.stderr)
    return None, f"{reason_tag}: insufficient confidence"


def _score(block: Block, full_path: str,
           preexisting_block: Block) -> float:
    """Higher = more confident `block` belongs to `full_path`."""
    score = 0.0
    hp = block.header_path or ""
    cb = block.context_before or ""

    # ---- path matching ----
    if hp == full_path:
        score += 100.0
    elif hp != os.path.basename(full_path) and full_path.endswith("/" + hp):
        score += 50.0

    # ---- context hints ----
    parts = full_path.split("/")
    for i in range(len(parts)):
        suffix = "/".join(parts[i:])
        if len(suffix) > len(hp) and suffix in cb:
            score += 40.0 - 3.0 * i
            break

    for seg in parts[:-1]:
        if len(seg) > 3 and seg in cb:
            score += 5.0

    # ---- content similarity ----
    if block.kind == "full":
        score += 30.0 * _line_jaccard(block.body, preexisting_block.body)
    elif block.kind == "partial":
        score += 15.0 * _line_jaccard(block.body, preexisting_block.body)
        score += 15.0 * _token_presence(block.body, preexisting_block.body)

    return score


def _line_jaccard(a: str, b: str) -> float:
    la = {ln.strip() for ln in a.splitlines() if ln.strip()}
    lb = {ln.strip() for ln in b.splitlines() if ln.strip()}
    if not la or not lb:
        return 0.0
    inter = len(la & lb)
    union = len(la | lb)
    return inter / union if union else 0.0


_TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")


def _token_presence(text: str, base: str) -> float:
    tokens = set(_TOKEN_RE.findall(text))
    if not tokens:
        return 0.0
    base_tokens = set(_TOKEN_RE.findall(base))
    return len(tokens & base_tokens) / len(tokens)
