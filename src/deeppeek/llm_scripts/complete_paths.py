"""llm_scripts.complete_paths — expand short filenames to paths.

Fully algorithmic — no LLM call.  Sets `requires_llm = False`; the
editor framework skips `client.call` and invokes only
`on_response`, which does the work here.

Input is whitespace-separated short filenames (basenames), e.g.:

    bg-onmessage.js bg-main.js bg-handlers-hijack.js content.js _resume.js

Algorithm:
  1. Walk up from cwd to find the project root (`pyproject.toml` /
     `package.json` / `.git` / etc., stopping at $HOME).
  2. Walk the tree under the root ONCE, building a
     `basename → [Path]` index.  Noise directories are skipped
     (`.git`, `node_modules`, `__pycache__`, `.venv`, `target`, …).
  3. For each input basename:
       - 0 matches → warn, emit the bare name as-is
       - 1 match   → emit it (unambiguous)
       - N matches → pick the candidate sharing the longest
                     directory prefix with the CONFIRMED set (the
                     single-match cases from this same batch).
                     Short-name batches cluster in one subtree.
  4. Convert project-root-relative paths to cwd-relative for
     output.  `--paths_absolute` prepends the project root instead,
     producing filesystem absolutes.

Output is directly usable from the caller's cwd:

    cd /proj                        → src/deeppeek/extension/bg-main.js
    cd /proj/src/deeppeek           → extension/bg-main.js
    cd /proj/src/deeppeek/extension → bg-main.js

Pipeline behaviour: when `ctx["is_last"]` is False, the result is a
JSON-serializable `{"paths": [...]}` dict so downstream scripts
(e.g. `cat_files`) can iterate programmatically.  When this script
is the last stage, the user-facing newline-joined string is
returned instead.

`--paths_absolute` is registered via `register_flags` and exists
only when `--script complete_paths` is on the argv.
"""
import os
import sys
from pathlib import Path

NAME = "complete_paths"
DESCRIPTION = (
    "Expand short filenames to paths relative to the caller's cwd "
    "(absolute with --paths_absolute).  Algorithmic — no LLM call."
)

# Framework hook: skip client.call entirely.
requires_llm = False


# ---- project root detection --------------------------------------

_PROJECT_MARKERS = (
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "build.gradle.kts",
    ".git",
)

_root_cache = {"cwd": None, "root": None}


def _find_project_root(start=None):
    """Walk up from `start` (default: cwd) to the first ancestor
    containing any `_PROJECT_MARKERS` file.  Stops at $HOME so a
    stray `~/.git` cannot capture every project.  Falls back to
    `start` when no marker is found."""
    start = Path(start or os.getcwd()).resolve()
    if _root_cache["cwd"] == start and _root_cache["root"] is not None:
        return _root_cache["root"]

    home = Path.home().resolve()
    current = start
    root = start
    while True:
        if current != home:
            if any((current / m).exists() for m in _PROJECT_MARKERS):
                root = current
                break
        if current == home:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    _root_cache["cwd"] = start
    _root_cache["root"] = root
    return root


# ---- basename index ----------------------------------------------

_SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "env",
    "target", "dist", "build", "out",
    ".tox", ".nox",
    ".idea", ".vscode",
}

_index_cache = {"root": None, "index": None}


def _walk_index(root):
    """One `os.walk` over `root` → `basename → [Path]`.

    Skips `_SKIP_DIRS` noise directories.  Each candidate list is
    sorted so ordering is deterministic across runs.  Cached per
    root, so daemon mode does not re-walk on every click.
    """
    if _index_cache["root"] == root and _index_cache["index"] is not None:
        return _index_cache["index"]

    index = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            index.setdefault(fname, []).append(Path(dirpath) / fname)
    for k in index:
        index[k].sort()

    _index_cache["root"] = root
    _index_cache["index"] = index
    return index


# ---- disambiguation ----------------------------------------------

def _pick_closest(candidates, confirmed, root):
    """From `candidates`, pick the one sharing the longest
    directory prefix with any `confirmed` match.

    `confirmed` comes from unambiguous basenames in the same input
    batch — short-name batches tend to cluster in one subtree, so
    the candidate adjacent to confirmed matches is almost always
    the right pick.  Falls back to `candidates[0]` (sorted, so
    deterministic) when `confirmed` is empty.
    """
    if not confirmed:
        return candidates[0]
    best, best_score = candidates[0], -1
    for cand in candidates:
        cand_dirs = cand.relative_to(root).parts[:-1]
        score = 0
        for conf in confirmed:
            conf_dirs = conf.relative_to(root).parts[:-1]
            common = 0
            for a, b in zip(cand_dirs, conf_dirs):
                if a == b:
                    common += 1
                else:
                    break
            score = max(score, common)
        if score > best_score:
            best_score, best = score, cand
    return best


# ---- solver ------------------------------------------------------

def _solve(input_text, ctx):
    """Resolve basenames → path strings.  Returns `list[str]`.

    Splitting the solver from `on_response` keeps the pipeline
    wrapping (`{"paths": [...]}` vs newline-joined string) trivial
    and one-liner-able.
    """
    root = _find_project_root()
    cwd = Path(os.getcwd())
    absolute = ctx.get("paths_absolute", False)

    print(f"  [complete_paths] project_root={root}", file=sys.stderr)
    print(f"  [complete_paths] cwd={cwd}", file=sys.stderr)

    basenames = input_text.split() if isinstance(input_text, str) else []
    if not basenames:
        return []

    index = _walk_index(root)
    resolved = [(name, index.get(name, [])) for name in basenames]
    confirmed = [cands[0] for _, cands in resolved if len(cands) == 1]

    out_lines = []
    for name, cands in resolved:
        if not cands:
            print(f"  [complete_paths] warning: {name!r} not found "
                  f"under {root}", file=sys.stderr)
            out_lines.append(name)
            continue
        if len(cands) == 1:
            chosen = cands[0]
        else:
            chosen = _pick_closest(cands, confirmed, root)
            print(f"  [complete_paths] disambiguated: {name!r} → "
                  f"{chosen.relative_to(root)} "
                  f"({len(cands)} candidates)",
                  file=sys.stderr)

        if absolute:
            out_lines.append(str(chosen))
        else:
            out_lines.append(os.path.relpath(chosen, cwd))

    return out_lines


# ---- framework hooks ---------------------------------------------

def register_flags(parser):
    parser.add_argument(
        "--paths_absolute", "--paths-absolute",
        action="store_true", default=False,
        dest="paths_absolute",
        help="(complete_paths) return absolute filesystem paths "
             "instead of cwd-relative paths",
    )


def ctx_from_args(args):
    return {"paths_absolute": args.paths_absolute}


def build_prompt(input_value, ctx):
    # requires_llm = False, so the framework skips client.call and
    # the returned string is never sent anywhere.  Stash the input
    # for on_response and return empty.
    ctx["_cp_input"] = input_value
    return ""


def on_response(response, ctx):
    input_value = ctx.pop("_cp_input", "")
    lines = _solve(input_value, ctx)

    if ctx.get("is_last", True):
        return "\n".join(lines)
    return {"paths": lines}
