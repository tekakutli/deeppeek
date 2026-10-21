"""
recover.attribute — Pass 4 (attribute orphan fences to a file).

DIFFICULT.  Owns:

    FIX [3]   Aggressive prose attribution: when 2+ files are named in
              the prose, pick the one whose name appears closest to the
              fence (the "nearest mention" wins, provided the runner-up
              is meaningfully further back).  The previous logic
              required exactly one match and dropped the orphan
              otherwise.
"""

import sys
from typing import Optional

from .core import Block, FileRecord, _basename


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

        b.attributed_by = "unattributed"


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
