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
             similarity + context hints + topic weight; commit only if
             the best score is confidently ahead of the runner-up.
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

SIBLING-DIRECTORY VOTING (2026-11-03)
=====================================
`pg_base.py` appears seven times in the playground tree:
`playgrounds/board/`, `playgrounds/cable/`, `playgrounds/cone/`,
`playgrounds/inpaint/`, `playgrounds/pair/`, `playgrounds/room/`,
`playgrounds/sam3_segment/`.  A conversation that names the file
without a directory path but is otherwise about the inpaint
playground still had to guess, and content-similarity scoring alone
could not discriminate because all seven files are structurally
similar (each is `PG_PANEL_BODY = r\"\"\"...\"\"\"` plus
`PG_CSS = r\"\"\"...\"\"\"`).

The resolver now tallies directories from its *unambiguous*
resolutions in a first pass, then uses the most-voted directory as
a tie-breaker during the second pass.

CONVERSATION-TOPIC WEIGHTS (2026-11-05)
=======================================
`dir_votes` handles the case where the conversation *itself*
resolves several sibling files unambiguously: those votes then break
ties for the ambiguous ones.  It does NOT handle the case where the
conversation's prose names a directory without going through the
resolver (e.g. "the inpaint playground" in the first user message),
or where the ambiguous files' unambiguous siblings happen to be
outside the target directory (a `kritomatic_daemon/` load votes for
that directory, but a `playgrounds/inpaint/` patch has no direct
vote unless `inpaint_server.py` is also in the same conversation).

`topic_weights` (produced by `recover.topic.infer_topic_weights`)
fills that gap: it is a precomputed `{relative_dir: weight}` dict
derived from the whole dump.  `_discriminate` adds
`topic_weights[dir] * 20.0` to each candidate's total score.  When
the topic bonus alone is >= 20 points, the gap threshold for
confident resolution drops from 10 to 5 — the topic signal is
strong enough to override the marginal-confidence guard without
the false-positive risk of a bare content tie.

With no topic_weights (default), scoring is identical to the
pre-2026-11-05 version.
"""
import os
import re
import sys
from typing import Optional

from .core import Block, _basename


_MIN_FULL_SUFFIX = 2  # "/" + header must appear in full_path


def resolve_paths(blocks: list[Block],
                  apply_to: Optional["Path"],
                  verbose: bool = False,
                  topic_weights: Optional[dict] = None) -> list[dict]:
    """Assign Block.output_path on every block in place.

    Returns a list of `{idx, header_path, resolved_to, reason}` records
    for the manifest — one per resolved block, useful for auditing
    which decisions were made.

    Two-pass:
      · First pass resolves only blocks whose match is unambiguous
        (exact, unique suffix, unique basename).  Each resolution
        votes for the directory of the resolved path.
      · Second pass resolves the rest, using the directory votes as
        a tie-breaking hint before falling back to content-similarity
        scoring (plus the topic_weights bonus).
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

    # ---- first pass: resolve unambiguous blocks, tally directories ----
    from collections import Counter
    dir_votes: Counter = Counter()
    unambiguous: dict[int, tuple[str, str]] = {}
    for b in blocks:
        if b.classified_by == "preexisting" or b.is_orphan:
            continue
        hp = b.header_path
        if not hp:
            continue
        resolved, reason = _resolve_one(
            b, preexisting_by_full, preexisting_by_basename, verbose,
            dir_votes={}, topic_weights=topic_weights)
        if resolved is not None and reason in (
                "exact", "suffix", "basename"):
            unambiguous[b.idx] = (resolved, reason)
            dir_votes[os.path.dirname(resolved)] += 1

    # ---- second pass: resolve the rest, using dir_votes + topic ----
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

        if b.idx in unambiguous:
            resolved, reason = unambiguous[b.idx]
            b.output_path = resolved
            resolutions.append({
                "idx": b.idx,
                "header_path": hp,
                "resolved_to": resolved,
                "reason": reason,
            })
            continue

        resolved, reason = _resolve_one(
            b, preexisting_by_full, preexisting_by_basename, verbose,
            dir_votes=dir_votes, topic_weights=topic_weights)
        if resolved is not None:
            b.output_path = resolved
            entry = {
                "idx": b.idx,
                "header_path": hp,
                "resolved_to": resolved,
                "reason": reason,
            }
            if topic_weights:
                entry["topic_dir"] = os.path.dirname(resolved)
                entry["topic_weight"] = round(
                    topic_weights.get(
                        os.path.dirname(resolved), 0.0), 2)
            resolutions.append(entry)
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
                 verbose: bool,
                 dir_votes=None,
                 topic_weights=None) -> tuple[Optional[str], str]:
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
                                 "suffix-ambiguous",
                                 dir_votes=dir_votes,
                                 topic_weights=topic_weights)

    # 3. Basename
    candidates = preexisting_by_basename.get(hp_base, [])
    if len(candidates) == 1:
        if verbose:
            print(f"  [resolve] idx={block.idx} {hp!r} → "
                  f"{candidates[0]} (unique basename)", file=sys.stderr)
        return candidates[0], "basename"
    if len(candidates) > 1:
        return _discriminate(block, candidates, preexisting_by_full,
                             verbose, "basename-ambiguous",
                             dir_votes=dir_votes,
                             topic_weights=topic_weights)

    return None, "no match"


def _discriminate(block: Block, candidates: list[str],
                  preexisting_by_full: dict[str, Block],
                  verbose: bool,
                  reason_tag: str,
                  dir_votes=None,
                  topic_weights=None) -> tuple[Optional[str], str]:
    # Directory votes are a BONUS, not a filter.  Every candidate
    # gets +15 per unambiguous sibling that resolved to its directory,
    # so a `pg_base.py` candidate in `playgrounds/inpaint/` gets +30
    # from the 2 `inpaint_server.py` blocks, while the sam3_segment
    # candidate gets 0.  Content similarity decides when the bonus is
    # tied; the bonus breaks ties the same way a human would ("the
    # conversation already agreed on this directory twice").
    #
    # Topic weights are a second, orthogonal bonus derived from the
    # whole conversation's directory mentions.  Same shape: +20 per
    # point of topic_weight, with the topic gate dropping the gap
    # threshold only when the topic signal alone is strong.
    scored = []
    for full in candidates:
        content_score = _score(block, full, preexisting_by_full[full])
        dir_bonus = 0.0
        if dir_votes:
            d = os.path.dirname(full)
            dir_bonus = float(dir_votes.get(d, 0)) * 15.0
        topic_bonus = 0.0
        if topic_weights:
            d = os.path.dirname(full)
            topic_bonus = float(topic_weights.get(d, 0)) * 20.0
        scored.append((full, content_score, dir_bonus, topic_bonus,
                       content_score + dir_bonus + topic_bonus))

    scored.sort(key=lambda x: x[4], reverse=True)

    if len(scored) == 1:
        return scored[0][0], reason_tag

    (best, _bc, _bd, _bt, best_total) = scored[0]
    (_sb, _sc, _sd, _st, second_total) = scored[1]
    gap = best_total - second_total

    topic_ready = (topic_weights is not None
                   and _bt >= 20.0)

    if best_total >= 20.0 and gap >= 10.0:
        if verbose:
            print(f"  [resolve] idx={block.idx} "
                  f"{block.header_path!r} {reason_tag}: chose "
                  f"{best} (score {best_total:.1f} vs "
                  f"{second_total:.1f})", file=sys.stderr)
        return best, f"{reason_tag} (score {best_total:.1f})"

    if topic_ready and gap >= 5.0:
        if verbose:
            print(f"  [resolve] idx={block.idx} "
                  f"{block.header_path!r} {reason_tag}: chose "
                  f"{best} (topic-aided; score {best_total:.1f} vs "
                  f"{second_total:.1f})", file=sys.stderr)
        return best, f"{reason_tag} (topic-aided score {best_total:.1f})"

    if verbose:
        print(f"  [resolve] idx={block.idx} "
              f"{block.header_path!r} {reason_tag}: no confident "
              f"choice among "
              f"{[(p, round(s, 1)) for p, _, _, _, s in scored]}",
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
    #
    # Two components beyond the historical jaccard:
    #
    # 1. Block-line density — what fraction of the block's own
    #    stripped lines already exist in the candidate body.
    #    Motivating case: seven `pg_state.py` files, one partial
    #    that replaces `fittedImageRect` + `clampPan`.  The candidate
    #    that already declares those two functions scores ~0.67 on
    #    density; every other candidate scores ~0.1.
    #
    # 2. Decl overlap — what fraction of the block's top-level
    #    declarations the candidate also declares.  For the same
    #    case, 2 of 2 declarations overlap against the correct
    #    candidate (1.0) and 0 of 2 against the others (0.0).
    #
    # A partial rewrite of a subset of a pre-existing file's
    # declarations is the common shape; the historical jaccard-only
    # score under-weights it (both bodies share many lines but the
    # union dilutes the signal).
    block_body = (block.body or "").strip()
    base_body = preexisting_block.body or ""

    if block_body and base_body:
        block_lines = {ln.strip() for ln in block_body.splitlines()
                       if ln.strip()}
        base_lines = {ln.strip() for ln in base_body.splitlines()
                      if ln.strip()}

        if block_lines and base_lines:
            line_density = (len(block_lines & base_lines)
                            / len(block_lines))
            score += 30.0 * line_density

        from .dedup import _top_level_decls
        block_decls = _top_level_decls(block_body)
        if block_decls:
            base_decls = _top_level_decls(base_body)
            decl_overlap = (len(block_decls & base_decls)
                            / len(block_decls))
            score += 25.0 * decl_overlap

    # Historical jaccard/token components retained for backward
    # compatibility on `full`-classified bodies (which enter the
    # scoring path only when they compete against pre-existing files
    # of the same name at a different directory).
    if block.kind == "full":
        score += 30.0 * _line_jaccard(block_body, base_body)
    elif block.kind == "partial":
        score += 15.0 * _line_jaccard(block_body, base_body)
        score += 15.0 * _token_presence(block_body, base_body)

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
