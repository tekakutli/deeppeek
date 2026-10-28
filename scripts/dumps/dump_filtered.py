#!/usr/bin/env python3
"""
dump_filtered.py — dump a project directory, filtered to files that
a conversation actually mentions.

Reads a markdown conversation dump (typically produced by
`deeppeek capture`), extracts every basename that appears anywhere
in the text — inside backticks, inside markdown link labels, inside
headers of any shape, or as a bare filename token in prose — then
runs `dump.py` (same directory) with an explicit file list, so only
files whose basenames match are included.

BASENAME MATCHING
=================
The filter is by BASENAME, not by path.  If the conversation names
`utils.py` and the project has both `src/utils.py` and
`lib/utils.py`, BOTH files are candidates.  The conversation may
not encode the real project layout, so any file that could be the
target is preserved.  This is the conservative choice: a false
positive costs one extra file in the dump; a false negative costs
a missing file the model would need to see.

SIBLING-DIRECTORY VOTING
========================
Naive "every file sharing the basename" is too permissive on
trees where common filenames repeat across sibling directories —
`pg_base.py` appears seven times in the kritomatic playgrounds,
`pg_state.py` four times, `pg_dispatch.py` six times.  When a
conversation is about *one* playground (say `playgrounds/inpaint/`)
but mentions `pg_state.py`, the naive rule drags in the six other
`pg_state.py` files as well.

Two-pass resolution fixes this:

  Pass 1 — every basename that appears exactly ONCE in the tree is
           unambiguous.  It is added to the result and casts a
           "vote" for its parent directory.

  Pass 2 — every basename with multiple matches is ambiguous.  It
           prefers the match(es) in the top-voted directory.  When
           votes tie (or there are no votes at all), the
           conservative "keep all matches" rule applies — the
           original "if there is a chance it is the file, it
           remains" contract is preserved for the ambiguous case
           that the votes cannot resolve.

Concrete effect on the kritomatic tree: the "inpaint playground"
conversation names `inpaint_server.py`, `pg_inpaint.py`, and
`pg_view_inpaint.py` (each unique — three votes for
`playgrounds/inpaint/`), plus `comfyui.py` and `comfyui_krita.py`
(each unique — one vote each for their daemon directories).  Every
other basename (`pg_state.py`, `pg_base.py`, …) is ambiguous but
has an inpaint-playground candidate; that candidate wins the vote
against the other directories (which have zero votes).  Result:
the 17 inpaint + daemon files the conversation is actually about,
instead of the 49 files the naive rule produced.

The same two-pass strategy is used by `deeppeek/recover/resolve.py`
for its own ambiguous-basename cases (see that module's
`SIBLING-DIRECTORY VOTING (2026-11-03)` docstring).  This module
stays standalone so it can be used outside deeppeek; the logic is
ported, not imported.

FULL-TREE WITH [skip] MARKERS
=============================
The dump's DIRECTORY TREE section is built from a full scan of the
project — not just the filtered files — so the user can see what
the project contains AND which files were excluded.  Every file
that matches `dump.py`'s extension filter appears in the tree;
files that are in the filtered set have no marker, files that were
excluded are suffixed with `[skip]`.

This is achieved by passing `--tree-from-scan` to `dump.py`.  The
FILE blocks section still only contains the filtered files — the
tree is purely a visual index that shows what wasn't included, not
a change to the dump's actual content.

EXTRACTION IS TOLERANT
======================
The previous version only scanned `## File: \\`x.py\\``-shaped
headers, so any other header shape — notably numbered list headers
like `## 1. \\`pg_state.py\\`` — produced zero matches and a silent
stub.  This version extracts filenames wherever they appear:

  · backtick spans: `## 1. \\`pg_state.py\\``, "the file \\`foo.py\\`"
  · markdown links:  [foo.py](https://…)
  · bare filename tokens in prose and in headers:
        ## File: foo.py   /   ## **foo.py**   /   "look at foo.py"

False positives (`os.path`, `1.5`, `self.foo.bar`) are filtered out
by an extension check against a set of known source extensions,
plus a curated list of extensionless names (`Makefile`,
`Dockerfile`, `LICENSE`, `.env`, …).  Everything else that looks
like a real source file is kept.  Matching is case-insensitive, so
`Foo.PY` in the conversation matches `foo.py` in the tree.

STANDALONE USE
==============
This script is self-contained (no deeppeek import, no other
project dependency).  It only needs `dump.py` beside it (which is
how it ships).  Run it directly to preview what a filtered dump
looks like before wiring it into `deeppeek prompt refine_deeppeek`:

    dump_filtered.py conversation.md
    dump_filtered.py conversation.md src/
    dump_filtered.py conversation.md -o /tmp/out.md
    dump_filtered.py conversation.md --list-only

PROGRAMMATIC USE
================
Call it as a subprocess with `-o PATH`:

    subprocess.run(
        [sys.executable, "dump_filtered.py",
         "conversation.md", "/path/to/project", "-o", "/tmp/out.md"],
        check=True,
    )

Exit code 0 = matches found and dump written.
Exit code 2 = no matches; a stub file explaining the mismatch is
              still written (so the caller can see why), but the
              caller should skip attaching it.
Exit code 1 = hard error (conversation unreadable, dump.py missing,
              dump.py failed, etc.).

WHY REUSE dump.py
=================
`dump.py` already has an "explicit file list" mode that bypasses
its extension / affix / include / exclude filters — what you list
is what you get.  The conversation's filename mentions ARE the
filter, so we don't need to reimplement dump.py's scanning.  We
only need to translate conversation mentions into a list of paths.
"""

import argparse
import importlib.util
import os
import re
import sys
from collections import Counter
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_OUT = Path("project_dump.md")  # base name; dirname gets appended
DUMP_SCRIPT_NAME = "dump.py"           # sibling file in this directory

# Directory names to skip when scanning the target.  Matches
# `deeppeek/recover/pipeline.py`'s pre-existing-scan exclude list
# so the filtered dump reflects the same "what counts as a source
# file" notion the pipeline uses when loading pre-existing
# baselines.
_EXCLUDE_DIR_NAMES = {
    "node_modules", "__pycache__", "dist", "build", "target",
    "venv", ".venv", ".tox", ".nox", ".git",
    "deeppeek", ".deeppeek",
}

# Files larger than this are skipped before the basename check —
# we don't want the filtered dump to fail on a 500MB log file just
# because the conversation mentions `latest.log`.
_MAX_FILE_BYTES = 1_048_576  # 1 MiB


# ---------------------------------------------------------------------------
# Filename extraction
# ---------------------------------------------------------------------------
#
# The previous version only scanned headers, and only recognised a fixed
# set of header forms (`## File: \`x.py\``, `## \`x.py\``, `## [x.py](url)`).
# Any other shape — notably numbered list headers like
#
#     ## 1. `pg_state.py`
#
# produced zero matches, which silently wrote a stub.  Rather than
# enumerating every header shape a given AI might use, this version
# extracts filenames wherever they appear (backticks, markdown links,
# header contents, bare prose) and lets the extension filter drop the
# false positives.  The caller then matches by basename — unchanged.

# Extensions we treat as "source files" for the purpose of the filter.
# An extension here means "a mention of `<name>.<ext>` should preserve
# `<name>.<ext>` in the tree".  Everything else (`1.5`, `os.path`,
# `node.foo`, `obj.bar`) is filtered out.
_SOURCE_EXTS = frozenset({
    "py", "pyi", "pyx",
    "js", "mjs", "cjs", "jsx",
    "ts", "tsx", "mts", "cts",
    "json", "toml", "yaml", "yml",
    "md", "markdown", "rst",
    "sh", "bash", "zsh", "fish",
    "css", "scss", "sass", "less",
    "html", "htm", "xml", "svg",
    "rs", "go", "c", "h", "cpp", "hpp", "cc", "cxx",
    "java", "kt", "kts", "scala", "groovy",
    "rb", "php", "pl", "pm", "lua",
    "swift", "dart", "m",
    "sql", "graphql", "gql", "proto",
    "vue", "svelte",
    "ini", "cfg", "conf", "env",
    "txt", "log",
})

# Extensionless filenames that are conventionally referenced by exact
# name.  Curated so that a `cat` mention in code doesn't accidentally
# match a `cat.py` file; only exact names land here.
_EXTENSIONLESS_NAMES = frozenset({
    "makefile", "gnumakefile",
    "dockerfile",
    "license", "licence", "copying", "notice",
    "readme", "changelog", "authors", "contributors",
    "install", "todo",
    "gemfile", "rakefile", "procfile", "vagrantfile",
    "brewfile", "pipfile", "podfile",
    ".gitignore", ".dockerignore", ".npmignore",
    ".editorconfig", ".env",
    ".python-version",
    ".npmrc", ".yarnrc", ".babelrc", ".eslintrc", ".prettierrc",
})

# A filename-like token: something with an internal `.` and a
# trailing 1–10 char extension.  Leading `.` is allowed so `.env`
# and `.editorconfig` match.  The lookbehind forbids matching inside
# a longer token (so `foo.pyx` doesn't match `foo.py`); the lookahead
# forbids matching a prefix (`foo.py` in `foo.pyc` doesn't match).
_FILENAME_TOKEN_RE = re.compile(
    r"(?<![\w.])"
    r"([A-Za-z0-9_.][A-Za-z0-9_\-./]*\.[A-Za-z0-9]{1,10})"
    r"(?![\w])"
)

# Anything inside backticks.  Used for prose references like
# "the file `foo.py`" and for headers like `## 1. \`pg_state.py\``.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# Markdown link labels: [foo.py](url).
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _looks_like_filename(name: str) -> bool:
    """True iff `name` looks like a source file we should preserve."""
    if not name:
        return False
    if name.lower() in _EXTENSIONLESS_NAMES:
        return True
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[1].lower()
    return ext in _SOURCE_EXTS


def _try_add(candidate: str, basenames: set[str]) -> None:
    """Extract a basename from `candidate` if it looks like a filename.

    Strips common wrappers — the `File:` prefix, a numbered list
    prefix, a trailing parenthetical like ` (corrected)`, and a
    trailing em-dash suffix like ` — add a "View" section` — before
    taking the basename.  The result is filtered by
    `_looks_like_filename`; a rejected candidate is silently
    dropped.
    """
    s = candidate.strip()
    if not s:
        return

    # "File:" / "File 1:" / "File 12 :" prefix.
    s = re.sub(r"^[Ff]ile\s*\d*\s*:\s*", "", s)

    # "1. foo.py" / "2) foo.py" numbered prefix.
    s = re.sub(r"^\d+\s*[.)]\s+", "", s)

    # "foo.py (corrected)" trailing parenthetical.
    s = re.sub(r"\s+\([^)]*\)\s*$", "", s)

    # "foo.py — add a 'View' section" trailing descriptive suffix.
    for sep in (" — ", " – ", " - "):
        if sep in s:
            s = s.split(sep, 1)[0]
            break

    base = os.path.basename(s)
    if not _looks_like_filename(base):
        return
    basenames.add(base)


def extract_basenames(conversation_text: str) -> set[str]:
    """Return the set of basenames mentioned anywhere in the text.

    Tolerant by construction: filenames are recognised inside
    backtick spans, markdown link labels, and bare filename tokens
    in prose (headers included).  A mention of `src/foo/bar.py`
    contributes `bar.py`; a mention of `bar.py` also contributes
    `bar.py`.  Basename matching in `_resolve_with_dir_votes` then
    applies this set to the tree, so any file whose basename appears
    here is kept.  This is the "if there is a chance it is the file,
    it remains" contract the caller asked for.
    """
    basenames: set[str] = set()

    # 1. Every backtick-wrapped span.  Covers `## 1. \`pg_state.py\``,
    #    `## File: \`foo.py\``, and prose like "the file `foo.py`".
    for m in _BACKTICK_RE.finditer(conversation_text):
        _try_add(m.group(1), basenames)

    # 2. Every markdown link label.  Covers `[foo.py](url)`.
    for m in _LINK_RE.finditer(conversation_text):
        _try_add(m.group(1), basenames)

    # 3. Every bare filename-like token.  Covers `## File: foo.py`,
    #    `## **foo.py**`, `1. foo.py` in prose, and bare prose
    #    mentions like "look at foo.py".  False positives from code
    #    blocks (`os.path`, `self.foo.bar`, `1.5`) are filtered by
    #    the extension check in `_looks_like_filename`.
    for m in _FILENAME_TOKEN_RE.finditer(conversation_text):
        _try_add(m.group(1), basenames)

    return basenames


# ---------------------------------------------------------------------------
# Target scan
# ---------------------------------------------------------------------------

def iter_source_files(root: Path):
    """Yield files under `root`, skipping excluded dirs and
    oversized files.  Matches `recover/pipeline._iter_text_files`'s
    contract so the filtered dump reflects the same "what counts as
    a source file" notion.

    Symlinks are followed (rglob follows by default) — a project
    that symlinks shared code still gets it in the dump.  Cycles
    are the user's problem; git repositories rarely have them.
    """
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part in _EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if any(part.startswith(".") and part not in (".env",)
               for part in rel.parts):
            continue
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield p


# ---------------------------------------------------------------------------
# Sibling-directory voting resolver
# ---------------------------------------------------------------------------
#
# Two passes:
#
#   1. Unique matches — basenames with exactly one file in the tree.
#      Each one is added to the result and its parent directory
#      receives one vote.
#
#   2. Ambiguous matches — basenames with multiple files.  Each one
#      prefers the file(s) in the top-voted directory.  Ties and
#      zero-vote scenarios fall through to "keep all matches" so the
#      conservative "if there is a chance it is the file, it
#      remains" contract is never violated by the vote itself.
#
# The vote never touches a basename that isn't already in
# `basenames`, so a false negative (dropping a file the model
# needed) is impossible from voting alone — the vote only narrows
# *within* an already-mentioned basename.

def _resolve_with_dir_votes(
    root: Path, basenames: set[str],
) -> tuple[list[Path], list[dict]]:
    """Resolve `basenames` to files under `root` using directory
    votes to disambiguate multiple matches.

    Returns (matched_paths, decisions):

      matched_paths — sorted list of Paths.
      decisions     — one dict per basename actually found in the
                      tree, describing how it was resolved.  Used by
                      --list-only and useful for debugging.
    """
    lower_set = {b.lower() for b in basenames}
    by_basename: dict[str, list[Path]] = {}
    for p in iter_source_files(root):
        base = p.name.lower()
        if base in lower_set:
            by_basename.setdefault(base, []).append(p)

    # ---- Pass 1: unique matches vote for their parent directory ----
    dir_votes: Counter = Counter()
    matched: list[Path] = []
    decisions: list[dict] = []
    for base, paths in by_basename.items():
        if len(paths) == 1:
            dir_votes[paths[0].parent] += 1
            matched.append(paths[0])
            decisions.append({
                "basename": base,
                "matched": [str(paths[0].relative_to(root))],
                "reason": "unique",
            })

    # ---- Pass 2: ambiguous matches use directory votes ----
    for base, paths in by_basename.items():
        if len(paths) == 1:
            continue

        candidate_dirs = {p.parent for p in paths}
        scored = sorted(
            ((dir_votes.get(d, 0), d) for d in candidate_dirs),
            key=lambda x: (-x[0], str(x[1])),
        )

        clear_winner = (
            len(scored) >= 2
            and scored[0][0] > 0
            and scored[0][0] > scored[1][0]
        )

        if clear_winner:
            winner_dir, winner_votes = scored[0][1], scored[0][0]
            winners = [p for p in paths if p.parent == winner_dir]
            matched.extend(winners)
            decisions.append({
                "basename": base,
                "matched": [str(p.relative_to(root)) for p in winners],
                "reason": f"dir-vote (score {winner_votes})",
                "all_candidates": [
                    str(p.relative_to(root)) for p in paths],
            })
        else:
            matched.extend(paths)
            decisions.append({
                "basename": base,
                "matched": [str(p.relative_to(root)) for p in paths],
                "reason": "ambiguous (kept all)",
            })

    return sorted(matched), decisions


def match_files(
    root: Path, basenames: set[str],
) -> list[Path]:
    """Return the files under `root` that `basenames` resolve to.

    Sibling-directory voting disambiguates multi-match cases:

      · A basename that appears exactly once (`inpaint_server.py`,
        `comfyui.py`) votes for its parent directory.

      · A basename that appears in multiple directories
        (`pg_state.py`, `pg_base.py`) then prefers the top-voted
        directory.

      · When votes tie (or there is no vote at all), all matches
        are kept — the conservative "if there is a chance it is
        the file, it remains" behaviour.

    For the "inpaint playground" conversation, this prunes the six
    other `pg_state.py` matches, the six other `pg_base.py` matches,
    etc., leaving the 17 inpaint + daemon files the conversation is
    actually about.

    This is a thin wrapper over `_resolve_with_dir_votes`; callers
    that want the per-basename decisions (for logging or
    `--list-only`) should call `_resolve_with_dir_votes` directly.
    """
    matched, _decisions = _resolve_with_dir_votes(root, basenames)
    return matched


# ---------------------------------------------------------------------------
# dump.py driver
# ---------------------------------------------------------------------------

def _find_dump_py(override: str | None) -> Path:
    if override:
        p = Path(override).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(
                f"error: --dump-py does not exist: {p}")
        return p
    sibling = Path(__file__).resolve().parent / DUMP_SCRIPT_NAME
    if not sibling.is_file():
        raise SystemExit(
            f"error: dump.py not found at {sibling}\n"
            f"  this script expects dump.py beside it, or pass "
            f"--dump-py PATH")
    return sibling


def _load_dump_module(dump_py: Path):
    """Load `dump.py` as a module without executing `main()`.

    Uses a fresh module object each call so re-entrancy (e.g. from
    a long-running caller) doesn't leak module-level state between
    invocations.
    """
    spec = importlib.util.spec_from_file_location(
        "dump_filtered_dump_instance", str(dump_py))
    if spec is None or spec.loader is None:
        raise SystemExit(
            f"error: could not load dump.py from {dump_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_dump(dump_py: Path, target: Path,
              files: list[Path], out: Path) -> int:
    """Invoke `dump.py` in explicit-files mode.

    dump.py's main() reads `sys.argv[1:]`, so we save/restore argv
    around the call.  We pass paths RELATIVE to `target` because
    dump.py resolves them relative to its `DEFAULT_DIR` (default
    ".").  Chdir-ing to `target` first makes the relative paths
    correct regardless of the caller's cwd.

    `--tree-from-scan` is passed so the DIRECTORY TREE section
    shows the full project tree, with `[skip]` markers on files
    that are not in the filtered set.  The FILE blocks section is
    unaffected — it still contains only the filtered files.
    """
    if not files:
        raise SystemExit(
            "error: _run_dump called with an empty file list")

    try:
        rel_paths = [str(f.relative_to(target)) for f in files]
    except ValueError as e:
        raise SystemExit(
            f"error: matched file outside target dir: {e}")

    mod = _load_dump_module(dump_py)

    old_cwd = Path.cwd()
    saved_argv = sys.argv
    try:
        os.chdir(str(target))
        sys.argv = [
            str(dump_py),
            "-o", str(out),
            "--tree-from-scan",
        ] + rel_paths
        rc = mod.main()
    finally:
        sys.argv = saved_argv
        os.chdir(str(old_cwd))
    return rc


# ---------------------------------------------------------------------------
# Stub (no matches)
# ---------------------------------------------------------------------------

_STUB_HEADER = (
    "# cwd dump (filtered to conversation-referenced files)\n"
    "#\n"
)


def _write_stub(out: Path, target: Path, conversation: Path,
                n_basenames: int) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        _STUB_HEADER
        + f"# No files in {target} matched any filename mention in\n"
        + f"# {conversation.name}.\n"
        + f"#\n"
        + f"# The conversation references {n_basenames} distinct "
          f"basename(s);\n"
        + f"# none of them resolve to a file in the target tree.\n"
        + "#\n"
        + "# Common causes:\n"
        + "#   · conversation targets a different checkout/branch\n"
        + "#   · conversation is a general chat with no file mentions\n"
        + "#   · files were deleted from the project after the chat\n"
        + "#\n"
        + "# Re-run with --list-only to inspect the extracted "
          f"basenames.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dump_filtered.py",
        description=(
            "Dump a project directory, filtered to files whose "
            "basenames appear anywhere in CONVERSATION (headers, "
            "backtick spans, link labels, or bare prose).  Wraps "
            "dump.py (same directory) in its explicit-files mode: "
            "the conversation's filename mentions ARE the filter.  "
            "The DIRECTORY TREE section shows the full project tree "
            "with `[skip]` markers on files that were filtered out."
        ),
    )
    ap.add_argument(
        "conversation", type=Path, metavar="CONVERSATION",
        help="markdown conversation dump; typically produced by "
             "`deeppeek capture`",
    )
    ap.add_argument(
        "target", type=Path, nargs="?", default=Path("."),
        metavar="DIR",
        help="project directory to scan (default: current directory)",
    )
    ap.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help=f"output path.  Default: {DEFAULT_OUT.stem}_<dir>"
             f"{DEFAULT_OUT.suffix} in the current directory.",
    )
    ap.add_argument(
        "--dump-py", type=str, default=None, metavar="PATH",
        help=f"override the dump.py script path "
             f"(default: {DUMP_SCRIPT_NAME} beside this script)",
    )
    ap.add_argument(
        "--list-only", action="store_true",
        help="print the extracted basenames and matched files to "
             "stdout, then exit without invoking dump.py.  Useful "
             "for debugging: shows whether extraction worked and "
             "whether the target tree contains the expected files.",
    )
    args = ap.parse_args(argv)

    conv: Path = args.conversation.resolve()
    if not conv.is_file():
        print(f"error: conversation not found: {conv}", file=sys.stderr)
        return 1

    target: Path = args.target.resolve()
    if not target.is_dir():
        print(f"error: target is not a directory: {target}",
              file=sys.stderr)
        return 1

    try:
        text = conv.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"error: cannot read {conv}: {e}", file=sys.stderr)
        return 1

    basenames = extract_basenames(text)
    print(f"[filter] {len(basenames)} unique basename(s) "
          f"extracted from {conv.name}", file=sys.stderr)
    if basenames:
        for b in sorted(basenames):
            print(f"  {b}", file=sys.stderr)

    matched, decisions = _resolve_with_dir_votes(target, basenames)
    print(f"[filter] {len(matched)} file(s) matched in {target}",
          file=sys.stderr)
    if matched:
        for p in matched[:20]:
            try:
                print(f"  {p.relative_to(target)}", file=sys.stderr)
            except ValueError:
                print(f"  {p}", file=sys.stderr)
        if len(matched) > 20:
            print(f"  … +{len(matched) - 20} more", file=sys.stderr)

    if args.list_only:
        # Human-readable resolution decisions to stderr.
        if decisions:
            print(f"\n[resolution] {len(decisions)} basename(s):",
                  file=sys.stderr)
        for d in decisions:
            reason = d["reason"]
            if "all_candidates" in d:
                print(f"  {d['basename']}  [{reason}]",
                      file=sys.stderr)
                for p in d["all_candidates"]:
                    marker = "*" if p in d["matched"] else " "
                    print(f"    {marker} {p}", file=sys.stderr)
            else:
                print(
                    f"  {d['basename']}  -> {d['matched'][0]}  "
                    f"[{reason}]",
                    file=sys.stderr,
                )
        # Machine-readable list on stdout for piping into other tools.
        for p in matched:
            try:
                print(p.relative_to(target))
            except ValueError:
                print(p)
        return 0

    # Resolve output path BEFORE deciding between stub and dump,
    # so both branches write to the same location.
    if args.output is not None:
        out = args.output.resolve()
    else:
        # Match dump.py's `<stem>_<dirname><suffix>` convention,
        # then append `_filtered` before the extension.
        dirname = target.name or "root"
        dirname = dirname.replace(os.sep, "_").replace("/", "_")
        out = Path(".") / f"{DEFAULT_OUT.stem}_{dirname}_filtered" \
                           f"{DEFAULT_OUT.suffix}"

    if not matched:
        _write_stub(out, target, conv, len(basenames))
        print(f"[filter] wrote stub to {out} "
              f"(no matches; see file for details)", file=sys.stderr)
        return 2

    dump_py = _find_dump_py(args.dump_py)
    print(f"[dump] invoking {dump_py.name} with "
          f"{len(matched)} explicit file(s)", file=sys.stderr)

    rc = _run_dump(dump_py, target, matched, out)
    if rc != 0:
        print(f"error: dump.py exited {rc}", file=sys.stderr)
        return 1

    if not out.is_file():
        print(f"error: dump.py did not write {out}", file=sys.stderr)
        return 1

    print(f"[filter] wrote {out} ({out.stat().st_size:,} bytes)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
