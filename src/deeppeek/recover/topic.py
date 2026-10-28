"""recover.topic — infer the conversation's target directory/directories.

Motivating case (2026-10-29 inpaint-playground run): every pg_*.py
patch has 5-7 candidates on disk; content scoring cannot discriminate
because the parallel playgrounds are structurally similar.  Without
topic inference, the resolver falls back to the literal header path,
records get keyed by bare basenames, the pre-existing baselines are
lost, and the patches never apply.

Signal sources, in descending order of reliability:

  · explicit directory mentions in the dump
    ("playgrounds/inpaint/", "kritomatic_daemon/utils/", …)
  · basename mentions of directory-unique files
    (inpaint_server.py appears only under playgrounds/inpaint/)
  · directory-basename mentions ("inpaint", "board", "cable") appearing
    within 200 chars of any block's header or filename reference

Only directories that exist in the target tree contribute to the final
dict; in --out mode (no target tree) this pass is a no-op.
"""
from __future__ import annotations

import os
import re
from collections import Counter


_DIR_TOKEN_RE = re.compile(r'(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+')


def infer_topic_weights(
    dump_text: str,
    blocks,
    existing_dirs: set[str],
) -> dict[str, float]:
    """Return {relative_dir: weight} for directories that exist in
    `existing_dirs`.  Higher weight = more likely this is the target
    directory for the conversation.

    `existing_dirs` is the set of relative directories that the
    target tree actually contains (e.g. {"playgrounds/inpaint",
    "playgrounds/board", "kritomatic_daemon/handlers", …}).
    """
    if not existing_dirs:
        return {}

    weights: Counter = Counter()

    # (1) Explicit full-path directory mentions
    lowered = dump_text.lower()
    for d in existing_dirs:
        rel = d.lower()
        n = lowered.count(rel + "/") + lowered.count(rel + "`")
        if n:
            weights[d] += 3.0 * n

    # (2) Directory-basename mentions, gated by co-mention of any
    # file that only lives in that directory
    basename_index: dict[str, set] = {}
    for d in existing_dirs:
        base = os.path.basename(d)
        if base and len(base) >= 4:
            basename_index.setdefault(base, set()).add(d)

    unique_file_votes = _unique_basename_votes(blocks, existing_dirs)
    for d, votes in unique_file_votes.items():
        weights[d] += float(votes)

    for base, dirs in basename_index.items():
        if len(dirs) > 1:
            # Ambiguous directory basename — still useful, but half
            # the weight of an unambiguous one.
            per_dir = 0.5
        else:
            per_dir = 1.0
        pat = re.compile(r'\b' + re.escape(base) + r'\b', re.IGNORECASE)
        total = len(pat.findall(dump_text))
        if total:
            for d in dirs:
                weights[d] += per_dir * total

    # (3) Co-mention votes: for each block whose prose names a file
    # that is unique to one directory, add a vote for that directory
    # within the block's context window.
    return dict(weights)


def _unique_basename_votes(blocks, existing_dirs) -> dict[str, float]:
    """For each basename that exists in exactly one directory,
    count how many blocks mention it.  Return {dir: vote_count}.
    """
    basename_to_dir: dict[str, str] = {}
    dir_to_basenames: dict[str, set] = {}
    for d in existing_dirs:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for name in entries:
            if not os.path.isfile(os.path.join(d, name)):
                continue
            base, ext = os.path.splitext(name)
            if ext not in (".py", ".js", ".mjs", ".ts", ".tsx"):
                continue
            dir_to_basenames.setdefault(d, set()).add(name)

    for d, names in dir_to_basenames.items():
        for name in names:
            if name in basename_to_dir and basename_to_dir[name] != d:
                basename_to_dir[name] = ""   # ambiguous
            else:
                basename_to_dir.setdefault(name, d)

    votes: Counter = Counter()
    for b in blocks:
        text = (b.context_before or "") + "\n" + (b.body or "")[:2000]
        for name, d in basename_to_dir.items():
            if d and name in text:
                votes[d] += 1
    return dict(votes)
