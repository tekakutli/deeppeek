"""
recover.attribute — Pass 4 (attribute orphan fences to a file).

DIFFICULT.  Owns:

    FIX [3]   Aggressive prose attribution: when 2+ files are named in
              the prose, pick the one whose name appears closest to the
              fence (the "nearest mention" wins, provided the runner-up
              is meaningfully further back).  The previous logic
              required exactly one match and dropped the orphan
              otherwise.

    FIX [4]   Scope-name attribution: when the prose names a function or
              class but no file (e.g. `## Fix 3 — surface per-command
              errors in `handle_inpaint``), look the scope up in every
              pre-existing file's body and attribute to the unique file
              that declares it.  Fail-closed on ambiguity (0 or 2+
              matches).

    FIX [5]   Content-similarity tiebreaker.
              When prose attribution or anchor attribution produces
              multiple candidates with equal footing, score each
              candidate by content overlap against the block's own body
              and pick the winner only if it beats the runner-up by a
              clear margin.

              Motivating case: `pg_base.py` appears seven times under
              `playgrounds/*/pg_base.py`, all with identical basenames.
              `_files_mentioned_in` returns all seven; nearest-mention
              computes the same `rfind` position for every path (same
              basename string), so no unique winner.

              Three scoring components, weighted equally (30/20/30),
              each normalized to [0, 1]:

                · line density — fraction of the block's stripped
                  lines that appear verbatim in the candidate's body.

                · declaration overlap — fraction of the block's
                  top-level declaration names that the candidate
                  also declares.

                · distinctive-token overlap — fraction of the
                  block's *distinctive* tokens (filtered for common
                  code-machinery vocabulary) that share a stem with
                  some candidate token.  Stem matching is
                  prefix-based with a compound-detection rule: `inpaint`
                  matches `inpaintPrompt` (compound suffix has an
                  uppercase letter) but not `inpainting` (all-
                  lowercase suffix — a different word).

              Threshold is 5 points; a margin below that returns None
              (fail-closed, caller falls through to the next
              heuristic).

    FIX [6]   Conversation-topic weighting (2026-11-05).
              Same signal and same shape as `resolve._discriminate`:
              `topic_weights[dir] * 20.0` added to each candidate's
              total.  When the topic bonus alone is >= 20 points, the
              gap threshold drops from 5 to 3 — a run whose
              conversation is clearly about one directory can
              disambiguate two candidates that differ only in that
              directory, without the risk of a stray tiebreak pulling
              the block into an unrelated tree.

              `pass4_attribute_orphans` threads `topic_weights`
              through from `pipeline.run`, which computes it once
              after classification.  No topic_weights = identical
              behaviour to the pre-2026-11-05 version.
"""
import os
import re
import sys
from typing import Optional

from .core import Block, FileRecord, _basename
from .prose_filter import filename_in_orphan_header


# ===========================================================================
#  Common code-machinery tokens (filter before stem matching)
# ===========================================================================
#
# A token that appears on this list contributes no signal to content
# similarity: both an inpaint playground and a board playground use
# `const`, `document`, `getElementById`.  If these counted as matches,
# every JS block would "match" every JS-containing candidate.  The
# list is deliberately conservative — it filters syntax and standard
# library names, not user vocabulary.
#
# Adding a name to the list is safe in one direction (fewer spurious
# matches) and harmful in another (a project whose domain is, say,
# `console` configuration would lose that signal).  Keep the list to
# genuinely cross-domain-standard terms.

_COMMON_CODE_TOKENS = frozenset({
    # JavaScript keywords
    "const", "let", "var", "function", "class", "return", "async",
    "await", "yield", "this", "new", "typeof", "instanceof", "delete",
    "void", "true", "false", "null", "undefined", "export", "import",
    "default", "extends", "super", "static", "get", "set",

    # JavaScript globals and DOM boilerplate
    "console", "window", "document", "globalThis", "self",
    "Math", "JSON", "Date", "Array", "Object", "String", "Number",
    "Boolean", "Promise", "Error", "Map", "Set", "Symbol",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "getElementById", "querySelector", "querySelectorAll",
    "addEventListener", "removeEventListener",
    "setAttribute", "getAttribute",
    "preventDefault", "stopPropagation",

    # Python keywords
    "def", "elif", "else", "finally", "except", "raise", "try",
    "with", "pass", "None", "True", "False", "self", "cls",
    "lambda", "and", "or", "not", "in", "is", "assert", "global",
    "nonlocal", "del",

    # Python builtins
    "print", "len", "range", "str", "int", "float", "dict", "list",
    "set", "tuple", "bool", "type", "isinstance", "issubclass",
    "getattr", "setattr", "hasattr", "iter", "next", "enumerate",
    "zip", "map", "filter", "sorted", "sum", "min", "max", "abs",
    "round", "open", "repr", "hash", "callable",

    # HTML/CSS structural names and common attribute values
    "div", "span", "id", "input", "textarea", "button", "select",
    "option", "type", "value", "name", "class",
    "style", "href", "src", "alt", "checkbox", "radio",
})


def _is_distinctive_token(t: str) -> bool:
    """A token is distinctive if it's long enough to be meaningful
    and isn't a standard code-machinery word.

    Two ranges: JS/Python builtins (filtered for both cases via the
    `t` set), and HTML/CSS words.  Case-sensitive filtering for
    identifiers (JS/Python are case-sensitive) but case-insensitive
    for HTML/CSS (where case rarely matters).
    """
    if len(t) < 4 or t.isdigit():
        return False
    if t in _COMMON_CODE_TOKENS:
        return False
    return True


# ===========================================================================
#  Stem matching
# ===========================================================================

def _stems_match(a: str, b: str, min_len: int = 4) -> bool:
    """True iff `a` and `b` name the same concept.

    Rules, in order:
      · Identical strings match.
      · One is a prefix of the other AND the shorter is at least
        `min_len` characters AND the suffix has at least one
        "compound marker" (uppercase letter, underscore, or digit).

    The compound-marker requirement is what distinguishes:
      · `inpaintPromptEl` ↔ `inpaintPrompt` (suffix "El", `E` is
        uppercase — compound, match)
      · `inpaint` ↔ `inpainting` (suffix "ing", all lowercase — a
        different word, no match)
      · `board` ↔ `board_member` (suffix "_member", underscore —
        compound, match)
      · `board` ↔ `boardroom` (suffix "room", all lowercase — no
        match)

    `min_len=4` prevents matches on short generic prefixes (`id` ↔
    `identifier` would be a false positive on a 2-char prefix; 4 is
    the empirical threshold below which English/identifier overlap
    becomes noise).

    Both arguments are typically ≥ 4 chars after filtering; the
    check is defensive.
    """
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < min_len:
        return False
    if not long.startswith(short):
        return False
    suffix = long[len(short):]
    if not suffix:
        return True
    return any(
        c.isupper() or c == "_" or c.isdigit()
        for c in suffix
    )


# ===========================================================================
#  Token extraction (quoted-vs-bare normalized)
# ===========================================================================

def _tokens_for_matching(text: str) -> set:
    """Identifier-and-literal tokens of `text`, with surrounding
    quotes stripped so string-literal tokens match bare identifiers.

    Example: in `getElementById("inpaintPrompt")`, the quoted form
    `"inpaintPrompt"` normalizes to `inpaintPrompt`, which then
    matches the bare `inpaintPrompt` in an HTML attribute
    `id="inpaintPrompt"`.

    Filtering by length and pure-digit-ness is applied here; the
    `_COMMON_CODE_TOKENS` filter is applied separately by
    `_distinctive_tokens` because the line-density and decl-overlap
    components operate on raw tokens (they don't need the common-code
    filter — lines and decls are their own filters).
    """
    if not text:
        return set()
    from .dedup import _TOKEN_RE
    out = set()
    for t in _TOKEN_RE.findall(text):
        if len(t) >= 2 and t[0] in ('"', "'", "`") and t[-1] == t[0]:
            t = t[1:-1]
        if len(t) >= 4 and not t.isdigit():
            out.add(t)
    return out


def _distinctive_tokens(text: str) -> set:
    """Tokens of `text` that are meaningful for content-similarity
    matching, i.e. not common code-machinery words.
    """
    return {
        t for t in _tokens_for_matching(text)
        if _is_distinctive_token(t)
    }


def _stem_match_count(block_distinctive: set,
                       base_distinctive: set) -> int:
    """Count of block-distinctive tokens that share a stem with
    some base-distinctive token.

    Each block token is counted at most once (first matching base
    token wins).  Multiple base tokens sharing the block token's
    stem count once — the block's token, not the base's, is the
    unit of measurement.
    """
    count = 0
    for bt in block_distinctive:
        for ct in base_distinctive:
            if _stems_match(bt, ct):
                count += 1
                break
    return count


# ===========================================================================
#  Base-content resolver (blocks_by_idx -> base body)
# ===========================================================================

def _base_content(rec: FileRecord, blocks_by_idx: dict) -> str:
    """Return the candidate's current content, or "" if unavailable.

    At pass 4 the record's `last_full_idx` has not yet been assigned
    (that happens in pass 5), but the record already has pre-existing
    baseline blocks in `source_blocks`.  Fall back to them so content-
    based disambiguation actually works during orphan attribution —
    without this fallback, `_pick_by_content` returns None for every
    candidate and the content-tiebreaker is dead code.
    """
    if rec.last_full_idx is not None:
        block = blocks_by_idx.get(rec.last_full_idx)
        if block is not None:
            return block.body or ""
    for idx in rec.source_blocks:
        block = blocks_by_idx.get(idx)
        if block is not None and block.classified_by == "preexisting":
            return block.body or ""
    return ""


# ===========================================================================
#  Pass 4
# ===========================================================================

def pass4_attribute_orphans(blocks: list[Block],
                             blocks_by_idx: dict[int, Block],
                             records: dict[str, FileRecord],
                             verbose: bool,
                             topic_weights: Optional[dict] = None) -> None:
    existing_paths = list(records.keys())

    for b in blocks:
        if not b.is_orphan:
            continue
        if b.kind == "recovered_meta":
            b.attributed_by = "skipped_meta"
            continue
        if b.kind == "not_a_file":
            b.attributed_by = "not_a_file"
            if not b.disposition:
                b.disposition = "orphan_not_code"
            continue
        if not b.body.strip():
            b.attributed_by = "empty"
            continue
        if not existing_paths:
            b.attributed_by = "no_candidates"
            continue

        # (a) prose names one existing file — or, if 2+ are named, the
        # one whose mention is closest to the fence.
        prose_candidates = _files_mentioned_in(b.context_before,
                                                existing_paths)
        if len(prose_candidates) == 1:
            _assign(b, prose_candidates[0], "prose", verbose)
            continue
        elif len(prose_candidates) > 1:
            ctx_lower = b.context_before.lower()
            scored = []
            for p in prose_candidates:
                base = _basename(p)
                if not base:
                    continue
                pos = ctx_lower.rfind(base.lower())
                if pos >= 0:
                    scored.append((pos, p))
            scored.sort(reverse=True)
            if len(scored) >= 2:
                best_pos, best_path = scored[0]
                second_pos = scored[1][0]
                if best_pos - second_pos > 20:
                    _assign(b, best_path, "prose_nearest", verbose)
                    continue
                best_content = _pick_by_content(
                    b, prose_candidates, records, blocks_by_idx,
                    topic_weights=topic_weights)
                if best_content is not None:
                    _assign(b, best_content, "prose_content", verbose)
                    continue
            elif len(scored) == 1:
                _assign(b, scored[0][1], "prose_nearest", verbose)
                continue

        # (b) leading declaration anchor
        # (a2) filename mentioned in the body's opening lines
        body_matches = filename_in_orphan_header(
            b.body, existing_paths)
        if len(body_matches) == 1:
            _assign(b, body_matches[0], "body_header", verbose)
            continue

        anchor = _extract_anchor(b.body)
        if anchor:
            anchor_matches = []
            for path in existing_paths:
                for src_idx in records[path].source_blocks:
                    src = blocks_by_idx.get(src_idx)
                    if src is None:
                        continue
                    if anchor in src.body:
                        anchor_matches.append(path)
                        break
            if len(anchor_matches) == 1:
                _assign(b, anchor_matches[0], "anchor", verbose,
                        extra=f"anchor={anchor}")
                continue
            if len(anchor_matches) > 1:
                best_content = _pick_by_content(
                    b, anchor_matches, records, blocks_by_idx,
                    topic_weights=topic_weights)
                if best_content is not None:
                    _assign(b, best_content, "anchor_content", verbose,
                            extra=f"anchor={anchor}")
                    continue

        # (c) verbatim subset
        subset_matches = []
        for path in existing_paths:
            for src_idx in records[path].source_blocks:
                src = blocks_by_idx.get(src_idx)
                if src is None:
                    continue
                if b.body.strip() in src.body:
                    subset_matches.append(path)
                    break
        if len(subset_matches) == 1:
            _assign(b, subset_matches[0], "subset", verbose)
            continue
        if len(subset_matches) > 1:
            best_content = _pick_by_content(
                b, subset_matches, records, blocks_by_idx,
                topic_weights=topic_weights)
            if best_content is not None:
                _assign(b, best_content, "subset_content", verbose)
                continue

        # (d) scope-name attribution.  When the prose names a function
        # or class but no file, look the scope up in every pre-existing
        # file's body.  Attribute to the unique file that declares it.
        if _try_scope_name_attribution(
                b, existing_paths, records, blocks_by_idx, verbose):
            continue

        # Unattributed blockquoted blocks are AI-thinking noise.
        if b.in_blockquote:
            b.attributed_by = "blockquote_skip"
        else:
            b.attributed_by = "unattributed"


def _pick_by_content(b: Block,
                     candidates: list[str],
                     records: dict[str, FileRecord],
                     blocks_by_idx: dict[int, Block],
                     threshold: float = 5.0,
                     topic_weights: Optional[dict] = None) -> Optional[str]:
    """Return the unique best candidate by content similarity, or None.

    Scores each candidate by the weighted sum of three components,
    each normalized to [0, 1] (see module docstring, FIX [5] for
    details and the motivating trace):

        line_density     × 30
        decl_overlap     × 20
        stem_token_overlap × 30

    Plus an optional `topic_weights[dir] * 20.0` bonus (FIX [6]).

    A winner is returned only when it beats the runner-up by at
    least `threshold` points (default 5) OR by 3 points with a
    strong (>= 20) topic bonus.  Fail-closed on ambiguity: 0 or 2+
    near-tied candidates returns None; the caller falls through to
    the next heuristic.
    """
    if not candidates or not b.body.strip():
        return None

    from .dedup import _top_level_decls

    block_body = b.body
    block_lines = {ln.strip() for ln in block_body.splitlines()
                   if ln.strip()}
    block_decls = _top_level_decls(block_body)
    block_distinctive = _distinctive_tokens(block_body)

    # If the block has *no* signal at all (no lines beyond blank, no
    # decls, no distinctive tokens), content similarity cannot
    # disambiguate.  Return None so the caller falls through.
    if not block_lines and not block_decls and not block_distinctive:
        return None

    scored = []
    for path in candidates:
        rec = records.get(path)
        if rec is None:
            continue
        base_body = _base_content(rec, blocks_by_idx)
        if not base_body:
            continue

        base_lines = {ln.strip() for ln in base_body.splitlines()
                      if ln.strip()}
        base_decls = _top_level_decls(base_body)
        base_distinctive = _distinctive_tokens(base_body)

        line_density = 0.0
        if block_lines and base_lines:
            line_density = len(block_lines & base_lines) / len(block_lines)

        decl_overlap = 0.0
        if block_decls:
            decl_overlap = len(block_decls & base_decls) / len(block_decls)

        stem_overlap = 0.0
        if block_distinctive and base_distinctive:
            matches = _stem_match_count(
                block_distinctive, base_distinctive)
            stem_overlap = matches / len(block_distinctive)

        content_score = (
            30.0 * line_density
            + 20.0 * decl_overlap
            + 30.0 * stem_overlap
        )

        topic_bonus = 0.0
        if topic_weights:
            d = os.path.dirname(path)
            topic_bonus = float(topic_weights.get(d, 0.0)) * 20.0

        scored.append((content_score + topic_bonus,
                       content_score, topic_bonus, path))

    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) == 1:
        return scored[0][3]

    best_total, _bc, best_topic, best_path = scored[0]
    second_total = scored[1][0]

    if best_total - second_total >= threshold:
        return best_path
    # Topic-aided path: the topic signal alone is strong enough to
    # lower the gap threshold.
    if best_topic >= 20.0 and best_total - second_total >= 3.0:
        return best_path
    return None


def _try_scope_name_attribution(b, existing_paths, records,
                                blocks_by_idx, verbose):
    try:
        from .patch_scope_insert import _extract_scope_name_from_prose
    except ImportError:
        return False

    scope = _extract_scope_name_from_prose(b.context_before or "")
    if not scope:
        scope = (b.scope_hint or "").strip()
    if not scope:
        return False

    if "." in scope:
        return False

    pat = re.compile(
        r"(?:^|\n)[ \t]*(?:async\s+)?(?:def|class|function)\s+"
        + re.escape(scope) + r"\b"
    )

    scope_matches = []
    for path in existing_paths:
        for src_idx in records[path].source_blocks:
            src = blocks_by_idx.get(src_idx)
            if src is None:
                continue
            if pat.search(src.body or ""):
                scope_matches.append(path)
                break

    if len(scope_matches) == 1:
        _assign(b, scope_matches[0], "scope_name", verbose,
                extra=f"scope={scope}")
        return True
    return False


def _assign(b: Block, path: str, method: str, verbose: bool,
            extra: str = "") -> None:
    b.attributed_to = path
    b.attributed_by = method
    b.canonical_path = path
    if verbose:
        tag = f"({extra})" if extra else f"({method})"
        print(f"  [pass 4] idx={b.idx} -> {b.canonical_path} {tag}",
              file=sys.stderr)


def _files_mentioned_in(prose: str, paths: list[str]) -> list[str]:
    mentioned = []
    for path in paths:
        base = _basename(path)
        if path in prose or (base and base in prose):
            mentioned.append(path)
    return mentioned


def _extract_anchor(body: str) -> Optional[str]:
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//", "/*", "*")):
            continue
        m = re.match(
            r"(?:export\s+(?:const|let|var|function|class)\s+"
            r"|(?:def|class|function)\s+"
            r"|(?:const|let|var)\s+)"
            r"(\w+)",
            line,
        )
        if m:
            return m.group(1)
        break
    return None
