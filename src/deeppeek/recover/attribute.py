"""
recover.attribute — Pass 4 (attribute orphan fences to a file).

DIFFICULT.  Owns:

    FIX [3]   Aggressive prose attribution: when 2+ files are named in
              the prose, pick the one whose name appears closest to the
              fence (the "nearest mention" wins, provided the runner-up
              is meaningfully further back).  The previous logic
              required exactly one match and dropped the orphan
              otherwise.

    FIX [4]   (NEW) Scope-name attribution: when the prose names a
              function or class but no file (e.g. `## Fix 3 — surface
              per-command errors in `handle_inpaint``), look the scope
              up in every pre-existing file's body and attribute to
              the unique file that declares it.  Fail-closed on
              ambiguity (0 or 2+ matches).
"""

import sys
from typing import Optional

from .core import Block, FileRecord, _basename
from .prose_filter import filename_in_orphan_header


def pass4_attribute_orphans(blocks: list[Block],
                             blocks_by_idx: dict[int, Block],
                             records: dict[str, FileRecord],
                             verbose: bool) -> None:
    existing_paths = list(records.keys())

    for b in blocks:
        if not b.is_orphan:
            continue
        if b.kind == "recovered_meta":
            b.attributed_by = "skipped_meta"
            continue
        if b.kind == "not_a_file":
            # Prose blocks arrive here with disposition already
            # set ("orphan_prose", from classify.py).  Non-prose
            # not-a-file (bash, json without a target) arrive
            # with disposition unset.  Preserve the specific
            # label; the fallback covers the non-prose case.
            # attributed_by=not_a_file excludes both from
            # write.py's unattributed warnings list.
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
            # FIX [3]: aggressive nearest-mention attribution
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

        # (d) NEW: scope-name attribution.  When the prose names a
        # function or class — e.g. "surface per-command errors in
        # `handle_inpaint`" — but names no file, look the scope up in
        # every pre-existing file's body.  Attribute to the unique
        # file that declares it.  Fail-closed on ambiguity (0 or 2+
        # matches → falls through to unattributed).  This catches the
        # natural-language header shape that would otherwise fail
        # closed on every reference.
        if _try_scope_name_attribution(
                b, existing_paths, records, blocks_by_idx, verbose):
            continue

        # Unattributed blockquoted blocks are AI-thinking noise:
        # skip silently rather than emitting an orphan warning for
        # every fragment the AI explored but did not commit to.
        # See Block.in_blockquote's docstring.
        if b.in_blockquote:
            b.attributed_by = "blockquote_skip"
        else:
            b.attributed_by = "unattributed"


def _try_scope_name_attribution(b: Block,
                                 existing_paths: list[str],
                                 records: dict[str, FileRecord],
                                 blocks_by_idx: dict[int, Block],
                                 verbose: bool) -> bool:
    """Try to attribute `b` by the scope name named in its prose.

    Returns True iff attribution was committed (either assigned or
    explicitly skipped as ambiguous — both are terminal outcomes for
    this block; falling through is only on "no scope name found").

    The scope name is extracted from the same prose patterns the
    anchor-insert heuristic uses, so a header like
        "## Fix 3 — surface per-command errors in `handle_inpaint`"
    yields `handle_inpaint`.  We then check that name as a top-level
    `def`/`class`/`function` declaration in each pre-existing
    baseline.  Exactly-one match → commit.  Any other count → fall
    through (caller will try subset attribution, then unattributed).

    Fail-closed on ambiguity, on an empty scope name, and on any
    import error from the downstream helpers.
    """
    try:
        from .patch_scope_insert import _extract_scope_name_from_prose
    except ImportError:
        return False

    scope = _extract_scope_name_from_prose(b.context_before or "")
    if not scope:
        return False

    # Scope names are identifiers, so a dotted extension suffix (e.g.
    # `pg_panel_scrubs` extracted from `pg_panel_scrubs.py`) is not
    # a real scope.  Filter those out — prose attribution or
    # body-header attribution would have already fired for a real
    # filename mention, so a bare-scope-word match here means the
    # extractor picked up the filename stem.  Skipping prevents the
    # scope-name path from firing on `In pg_panel_scrubs.py, wire
    # both:` where prose attribution already resolved the block.
    #
    # (That case runs *before* this function in the caller's flow,
    # so in practice we only see truly bare names here; the filter
    # is a defensive belt for the case where the caller's prose
    # scan missed a filename due to case or partial-path).
    if "." in scope:
        return False

    import re
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
    import re
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
