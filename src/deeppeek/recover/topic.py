"""recover.topic — infer the conversation's target directory/directories.

Motivating case (2026-10-29 inpaint-playground run): every pg_*.py
patch has 5-7 candidates on disk; content scoring cannot discriminate
because the parallel playgrounds are structurally similar.  Without
topic inference, the resolver falls back to the literal header path,
records get keyed by bare basenames, the pre-existing baselines are
lost, and the patches never apply.

Signal sources, in descending order of reliability:

  • explicit directory mentions in the dump
    ("playgrounds/inpaint/", "kritomatic_daemon/utils/", …)
  • basename mentions of directory-unique files
    (inpaint_server.py appears only under playgrounds/inpaint/)
  • directory-basename mentions ("inpaint", "board", "cable") *in
    prose only* — see PROSE-ONLY VOTING below.

Only directories that exist in the target tree contribute to the final
dict; in --out mode (no target tree) this pass is a no-op.

PROSE-ONLY VOTING (2026-10-30, extended 2026-11-05)
===================================================
The bare-directory-basename vote used to scan the entire dump.  That
was wrong: the dump's code bodies are dense with identifiers that
happen to match a directory's basename.  `playgrounds/board/pg_state.py`
declares `export const board = { … }`, and the conversation quotes that
file's body at length; `\bboard\b` therefore fires dozens of times per
run and drowns out `\binpaint\b`, which appears only in prose.  The
vote would then pick `playgrounds/board` over `playgrounds/inpaint` for
a conversation whose topic is unambiguously inpaint — which is exactly
what happened to blocks 636/637/638 (`pg_base.py`,
`pg_panel_scrubs.py`, `pg_panel_bindings.py`), sending them to the
board playground where they were applied to files that never needed
them.

The fix for the directory-basename vote was to scan prose only
(`Block.context_before`), which is what the extractor has already
separated out.  Code bodies do not carry topic signal — they carry
*identifier* signal, and the two must not be conflated.

The *unique-basename* vote (source 3) had a related, separate bug: it
scanned `b.body[:2000]` in addition to prose.  A single `# (see
write.py)` comment or a `"write.py"` string literal inside a code body
was enough to cast a vote.  In the 2026-10-28 inpaint conversation,
the code samples that quoted `playgrounds/cable/` files mentioned
cable-unique basenames (`write.py`, `pipeline.py`, `recover.py`,
`pack.py`, `service.py`, …) inside their bodies; each mention voted
for cable.  The resulting topic weight for `playgrounds/cable` was
49.0, versus 0 for `playgrounds/inpaint`, and the resolver committed
to cable for `pg_state.py` — which was never the right target.

The 2026-11-05 fix restricts the unique-basename vote to prose as
well, and uses a word-bounded match so a filename substring inside a
larger identifier (e.g. `write_pipeline.py` matching `pipeline.py`
as a substring) does not vote.  A filename in prose is still a
legitimate signal; a filename inside a code sample is not.

The explicit-full-path vote (source 1) is unchanged: nobody writes
`playgrounds/inpaint/` except to name a file in that directory, so
it is safe to scan the entire dump for that signal.
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

    # (1) Explicit full-path directory mentions.
    #
    # `playgrounds/inpaint/` is unambiguous; nobody writes that string
    # except to name a file in the directory.  Scanning the whole dump
    # is correct here — the pattern cannot false-positive on a variable
    # name.
    lowered = dump_text.lower()
    for d in existing_dirs:
        rel = d.lower()
        n = lowered.count(rel + "/") + lowered.count(rel + "`")
        if n:
            weights[d] += 3.0 * n

    # (2) Directory-basename mentions, from PROSE ONLY.
    #
    # The dump's code bodies contain many identifiers that happen to
    # share a directory's basename (`board` in `pg_state.py`,
    # `cone` in some geometry file, `pair` in some pairing helper).
    # Scanning code for `\bboard\b` biases the vote toward whichever
    # directory's name happens to appear as a variable in the code
    # being discussed, so we exclude code entirely.  `blocks` already
    # carries prose (`context_before`) separated from bodies; that is
    # the corpus this signal needs.
    prose_corpus = "\n".join(
        (b.context_before or "") for b in blocks if b.context_before
    )
    basename_index: dict[str, set] = {}
    for d in existing_dirs:
        base = os.path.basename(d)
        if base and len(base) >= 4:
            basename_index.setdefault(base, set()).add(d)

    for base, dirs in basename_index.items():
        if len(dirs) > 1:
            # Ambiguous directory basename — still useful, but half
            # the weight of an unambiguous one.
            per_dir = 0.5
        else:
            per_dir = 1.0
        pat = re.compile(r'\b' + re.escape(base) + r'\b', re.IGNORECASE)
        total = len(pat.findall(prose_corpus))
        if total:
            for d in dirs:
                weights[d] += per_dir * total

    # (3) Unique-basename votes: for each basename that exists in
    # exactly one directory, count how many blocks mention it *by
    # filename* (e.g. `inpaint_server.py`).  Filename in prose is the
    # signal here — a filename in a code body is not.
    #
    # FIX (2026-11-05): scan prose only, and word-bounded.  A code
    # body quoting `playgrounds/cable/write.py` used to vote for
    # cable via a `write.py` filename reference in prose; when a code
    # sample mentioned `write.py` as a string, it voted for cable too.
    # That inflated cable's topic weight to 49.0 in the 2026-10-28
    # inpaint conversation and pushed the resolver to pick
    # `playgrounds/cable/pg_state.py` over `playgrounds/inpaint/
    # pg_state.py`.  See the module docstring, PROSE-ONLY VOTING.
    unique_file_votes = _unique_basename_votes(blocks, existing_dirs)
    for d, votes in unique_file_votes.items():
        weights[d] += float(votes)

    return dict(weights)


def _unique_basename_votes(blocks, existing_dirs) -> dict[str, float]:
    """For each basename that exists in exactly one directory,
    count how many blocks mention it in their *prose*.  Return
    {dir: vote_count}.

    Prose-only by design — see the module docstring.  Word-bounded
    matching avoids treating `write_pipeline.py` as a mention of
    `pipeline.py`.
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
        # Prose only.  Code bodies are identifier-dense and produce
        # spurious votes for whichever directory happens to share a
        # common identifier.
        text = (b.context_before or "")
        if not text:
            continue
        for name, d in basename_to_dir.items():
            if not d:
                continue
            if re.search(r"\b" + re.escape(name) + r"\b", text):
                votes[d] += 1
    return dict(votes)
