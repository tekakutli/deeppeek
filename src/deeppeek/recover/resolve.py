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
derived from the whole dump.

CONTENT-FIRST DISCRIMINATION (2026-11-05)
=========================================
Prior to this change, `_discriminate` computed
`content_score + dir_bonus + topic_bonus` and sorted by the total.
Both bonuses are scale-unbounded: `dir_votes[d] * 15` and
`topic_weights[d] * 20`.  In the 2026-10-28 inpaint conversation,
the topic weight for `playgrounds/cable` was 49.0 (a
`topic.py` bug — see its PROSE-ONLY VOTING docstring).  That
produced `topic_bonus = 980.0` and total = `content_score + 980`,
which dwarfed the inpaint candidate's content score of ~20 and
guaranteed cable won.  The content signal alone was barely ~6 points
for cable; the resolver was effectively picking the directory on a
*prior* and dropping the evidence.

The 2026-11-05 fix restructures `_discriminate`:

  1. If any candidate's content score beats the runner-up by a
     large enough margin (15 points), that candidate wins on
     content alone.  The priors are ignored — they can only
     refine a close call, not override a clear one.

  2. Otherwise, both bonuses are computed *capped and normalised*:
       · `dir_bonus` is capped per-directory at 3 votes * 5 = 15
       · `topic_bonus` is normalised by the maximum candidate's
         topic weight, capped at +10 total
     The combined score then has a bounded bonus contribution of at
     most 25 points, small enough that 3+ matched declarations (25
     content points) can dominate it.

  3. If the combined margin is still small (<5), fail closed.

  The net effect: a wrong topic prior can cost the resolver a
  tie-break, but cannot hand it a clearly-wrong answer.  When
  content is ambiguous and topic is wrong, the outcome is "no
  confident choice" (which routes to the literal header path, the
  previous safe default), not "cable confidently."

The `topic.py` prose-only fix removes the specific trigger, but the
structural change in `_discriminate` is the real invariant: no
prior may dominate content.  Even a future bug in `topic.py` should
not be able to misroute a file whose content is clearly a match for
one candidate.

BASENAME-DIRECTORY COMPATIBILITY PRUNING (2026-11-05)
====================================================
When a block's header carries an explicit directory AND the basename
has candidates whose directory is unrelated, the resolver prunes
those candidates before the "unique basename" branch fires.  The
header's directory is strong evidence; matching on basename alone is
only legitimate when the header has no directory, or when the
candidate's directory is a suffix of (or equal to) the header's.

Motivating bug (2026-10-29): a header `src/deeppeek/recover/
statement_sig.py` resolved to `tests/recover/statement_sig.py`
because the basename matched a single candidate in tests/ and the
`src/deeppeek/` prefix was silently discarded.  The two new modules
landed in `tests/recover/`; every later `from .statement_sig import`
failed with ModuleNotFoundError, cascading 37 test failures.

RESOLUTION INVARIANT CHECK (2026-11-05)
=======================================
`resolve_paths` calls `_validate_resolution(header_path,
output_path, reason)` before writing each resolved path.  Three
invariants:

  · `reason` starting with "unresolved" → output_path MUST equal
    header_path (the fallback contract is "write it verbatim").

  · Resolved basenames MUST match header basenames (no renames).

  · When the header carries an explicit directory, the resolved
    path's directory MUST be compatible (equal, suffix-of, or a
    deeper-suffix-of).

A violation demotes the resolution to the literal-header fallback
and records the violation in the resolutions manifest, so a future
misrouting is visible on the first run rather than inferred from
test failures.
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
            # NEW (2026-11-05): resolution invariant check.  When
            # the resolver claims a confident match but the matched
            # path has a directory incompatible with an explicit
            # header, fall back to the literal header and warn.
            # Defense-in-depth: the pruning in `_resolve_one`
            # closes the motivating case; this invariant catches
            # any future case where evidence-loss recovers via a
            # different heuristic.
            _invariant_warn = _validate_resolution(hp, resolved, reason)
            if _invariant_warn:
                print(f"  [resolve] idx={b.idx}: {_invariant_warn}",
                      file=sys.stderr)
                b.output_path = hp
                resolutions.append({
                    "idx": b.idx,
                    "header_path": hp,
                    "resolved_to": resolved,
                    "reason": f"{reason} (INVARIANT VIOLATION; "
                              f"fallback to literal)",
                    "warning": _invariant_warn,
                })
                continue
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


def _validate_resolution(header_path: str,
                         output_path: str,
                         reason: str) -> Optional[str]:
    """Return a warning string if the resolution is structurally
    implausible, else None.

    Three invariants:

      · When `reason` starts with "unresolved", `output_path` MUST
        equal `header_path`.  The fallback's contract is "write it
        verbatim"; divergence means an upstream pass rewrote the
        path without the resolver knowing.

      · Resolved basenames MUST match header basenames.  A rename
        is not something this resolver supports.

      · When the header carries an explicit directory, the resolved
        path's directory MUST be compatible (equal, suffix-of, or a
        deeper-suffix-of).  An explicit `src/deeppeek/` in the
        header is strong evidence; a match that discards it has
        lost information.

    Motivating bug (2026-10-29): header `src/deeppeek/recover/
    statement_sig.py` resolved to `tests/recover/statement_sig.py`
    because the basename matched a single candidate in tests/ and
    the `src/deeppeek/` prefix was silently discarded.  The pruning
    in `_resolve_one` closes the specific case; this invariant is
    defense-in-depth for any future evidence-loss.
    """
    # NEW (2026-10-29): caller (`resolve_paths`) only invokes
    # this on real matches (`resolved is not None`); the
    # pre-fix "unresolved" branch was dead code.  The two
    # structural invariants below are what matter for a
    # successful resolution.
    if not header_path or not output_path:
        return None

    if _basename(header_path) != _basename(output_path):
        return (f"resolved basename differs from header: "
                f"{header_path!r} -> {output_path!r}")

    header_dir = os.path.dirname(header_path)
    output_dir = os.path.dirname(output_path)
    if not header_dir:
        return None
    if (output_dir == header_dir
            or output_dir.endswith("/" + header_dir)
            or header_dir.endswith("/" + output_dir)):
        return None
    return (f"resolved directory incompatible with header: "
            f"{header_path!r} -> {output_path!r} "
            f"(header dir {header_dir!r}, output dir "
            f"{output_dir!r} are unrelated)")


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

    # NEW (2026-11-05): prune basename candidates whose directory is
    # incompatible with an explicit header directory.  Motivating
    # bug: header `src/deeppeek/recover/statement_sig.py` resolved
    # to `tests/recover/statement_sig.py` because the basename
    # matched a single candidate in tests/ and the header's
    # `src/deeppeek/` prefix was silently discarded.  An explicit
    # directory is strong evidence; basename match alone is only
    # legitimate when the header has no directory, or the
    # candidate's directory is a suffix of (or equal to) the
    # header's.
    if candidates and hp != hp_base:
        header_dir = os.path.dirname(hp)
        compatible = []
        for c in candidates:
            c_dir = os.path.dirname(c)
            if (c_dir == header_dir
                    or c_dir.endswith("/" + header_dir)
                    or header_dir.endswith("/" + c_dir)):
                compatible.append(c)
        if compatible:
            candidates = compatible
        else:
            # No candidate is compatible — this is a "no match", not
            # an "ambiguous basename".  Returning zero candidates
            # pushes the caller to the literal-header fallback, which
            # is what we want: a header naming `src/.../X.py` should
            # never silently resolve to `tests/.../X.py`.
            candidates = []

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
    """Pick among ambiguous candidates, evidence first.

    Content score is the primary signal.  `dir_votes` and
    `topic_weights` are tie-breakers with bounded magnitude — they
    cannot override a clear content win, and combined they contribute
    at most ~25 points, so 3-4 matched declarations (25 content
    points) still dominate them.

    See the module docstring, CONTENT-FIRST DISCRIMINATION, for why
    this restructuring matters: unbounded priors let a wrong topic
    weight hand the resolver a 980-vs-20 win.
    """
    # Baseline scoring for each candidate.
    scored = []
    for full in candidates:
        content_score = _score(block, full, preexisting_by_full[full])
        scored.append([full, content_score, 0.0, 0.0, content_score])

    # Content-only short circuit: if the top content score clearly
    # beats the runner-up, commit on content alone.  Priors are not
    # consulted.  This is the invariant the module docstring describes
    # — no prior may override a clear content win.
    content_sorted = sorted(scored, key=lambda x: x[1], reverse=True)
    if len(content_sorted) == 1:
        return content_sorted[0][0], reason_tag

    best_c = content_sorted[0][1]
    second_c = content_sorted[1][1]
    if best_c - second_c >= 15.0:
        if verbose:
            print(f"  [resolve] idx={block.idx} "
                  f"{block.header_path!r} {reason_tag}: chose "
                  f"{content_sorted[0][0]} (content-only, gap "
                  f"{best_c - second_c:.1f})", file=sys.stderr)
        return (content_sorted[0][0],
                f"{reason_tag} (content, gap {best_c - second_c:.1f})")

    # Content is close.  Compute capped, normalised priors.
    #
    #   dir_bonus:  +5 per unambiguous sibling resolution to the
    #               candidate's directory, capped at +15 (3 siblings).
    #   topic_bonus: scaled by the max candidate's topic weight so
    #                the best-scoring directory gets +10 total.
    max_topic = 0.0
    if topic_weights:
        for full in candidates:
            w = float(topic_weights.get(os.path.dirname(full), 0.0))
            if w > max_topic:
                max_topic = w

    for entry in scored:
        full = entry[0]
        d = os.path.dirname(full)
        if dir_votes:
            vote_count = min(float(dir_votes.get(d, 0)), 3.0)
            entry[2] = vote_count * 5.0
        if topic_weights and max_topic > 0:
            w = float(topic_weights.get(d, 0.0))
            entry[3] = (w / max_topic) * 10.0
        entry[4] = entry[1] + entry[2] + entry[3]

    scored.sort(key=lambda x: x[4], reverse=True)

    (best, _bc, _bd, best_tb, best_total) = scored[0]
    (_sb, _sc, _sd, _st, second_total) = scored[1]
    gap = best_total - second_total

    if best_total >= 10.0 and gap >= 10.0:
        if verbose:
            print(f"  [resolve] idx={block.idx} "
                  f"{block.header_path!r} {reason_tag}: chose "
                  f"{best} (combined {best_total:.1f} vs "
                  f"{second_total:.1f})", file=sys.stderr)
        return best, f"{reason_tag} (combined {best_total:.1f})"

    if best_tb >= 5.0 and gap >= 5.0:
        if verbose:
            print(f"  [resolve] idx={block.idx} "
                  f"{block.header_path!r} {reason_tag}: chose "
                  f"{best} (topic-aided; {best_total:.1f} vs "
                  f"{second_total:.1f})", file=sys.stderr)
        return best, f"{reason_tag} (topic-aided {best_total:.1f})"

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
