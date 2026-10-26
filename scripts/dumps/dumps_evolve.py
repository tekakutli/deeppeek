#!/usr/bin/env python3
"""
dumps_evolve.py — delta dump for iterative LLM review.

Modes:

  dumps_evolve.py
      Compare current working tree against a reference, and write:
        baseline.md   files UNCHANGED since reference
        delta.md      files CHANGED or ADDED in the working tree
      Reference = latest.md if it exists, otherwise git HEAD.

  dumps_evolve.py --latest FILE
      Adopt FILE as latest.md (validated + moved into place), then run as
      above. Use this to bootstrap from an external dump.

  dumps_evolve.py DELTA LATEST
      Merge two dumps into a new latest.md. LATEST is the base; DELTA
      overrides on path collision.

      Prefer (baseline.md, delta.md): reconstructs the current state exactly.
      (delta.md, latest.md) also works, but files DELETED in cwd will linger
      from latest.md, since delta carries no "deleted" record.

Typical workflow:

  1.  dumps_evolve.py                         # diff cwd vs HEAD
  2.  review baseline.md / delta.md (paste to LLM, etc.)
  3.  dumps_evolve.py baseline.md delta.md    # -> latest.md (snapshot of cwd)
  4.  edit code ...
  5.  dumps_evolve.py                         # diff cwd vs latest.md
  6.  repeat from 3

Configuration (top of file):
  OVERRIDES   dump.py module variables to override (same keys as dumps_compare)
  DUMP_ARGS   extra CLI args for dump.py (default ["-r"])
"""

import argparse
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
BASELINE_PATH = Path("baseline.md")
DELTA_PATH    = Path("delta.md")
LATEST_PATH   = Path("latest.md")
DEFAULT_REF   = "HEAD"

SCRIPT_DIR     = Path(__file__).resolve().parent
DUMP_SCRIPT    = SCRIPT_DIR / "dump.py"
COMPARE_SCRIPT = SCRIPT_DIR / "dumps_compare.py"
DIFF_SCRIPT    = SCRIPT_DIR / "dumps_diff.py"

OVERRIDES = {
    # "DEFAULT_EXTENSIONS": [".py", ".sh"],
    # "DEFAULT_RECURSIVE": True,
}

DUMP_ARGS = ["-r"]
# ---------------------------------------------------------------------------

_cache = {}


def _load(name, path):
    if name in _cache:
        return _cache[name]
    if not path.is_file():
        raise RuntimeError(f"required script not found: {path}")
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _cache[name] = mod
    return mod


def load_dump():    return _load("evolve_dump",    DUMP_SCRIPT)
def load_compare(): return _load("evolve_compare", COMPARE_SCRIPT)
def load_diff():    return _load("evolve_diff",    DIFF_SCRIPT)


# --- Dumping ---------------------------------------------------------------

def _parse_dump_args():
    """Extract (recursive_override, affix_override) from DUMP_ARGS."""
    recursive, affix = None, None
    args = list(DUMP_ARGS)
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-r", "--recursive", "-t", "--tree-only"):
            recursive = True
        elif a in ("-a", "--affix") and i + 1 < len(args):
            affix = args[i + 1]; i += 1
        elif a.startswith("--affix="):
            affix = a.split("=", 1)[1]
        i += 1
    return recursive, affix


def dump_dir(scan_root):
    """Run dump.py's filters over `scan_root`; return [(rel_path, content)]."""
    dm = load_dump()
    saved = {}
    for k, v in OVERRIDES.items():
        saved[k] = getattr(dm, k, None)
        setattr(dm, k, v)
    try:
        scan_root = Path(scan_root)
        root = scan_root / (getattr(dm, "DEFAULT_DIR", ".") or ".")
        recursive_ovr, affix_ovr = _parse_dump_args()

        exts = [dm.normalize_ext(x) for x in getattr(dm, "DEFAULT_EXTENSIONS", [])]
        exts = [e for e in exts if e]
        affix = affix_ovr if affix_ovr is not None \
                else getattr(dm, "DEFAULT_AFFIX", None)
        if affix == "":
            affix = None
        excl_dirs = [d for d in getattr(dm, "DEFAULT_EXCLUDE_DIRS", []) if d]
        incl_dirs = {d for d in getattr(dm, "DEFAULT_INCLUDE_DIRS", []) if d}
        incl_sub  = [s for s in getattr(dm, "DEFAULT_INCLUDE_FILE_SUBSTRINGS", []) if s]
        excl_sub  = [s for s in getattr(dm, "DEFAULT_EXCLUDE_FILE_SUBSTRINGS", []) if s]
        recursive = recursive_ovr if recursive_ovr is not None \
                    else getattr(dm, "DEFAULT_RECURSIVE", False)

        paths = dm.find_files(root, exts, affix, excl_dirs, incl_dirs,
                              incl_sub, excl_sub, recursive)
        result = []
        for p in paths:
            try:
                rel = os.path.relpath(str(p), str(scan_root))
            except ValueError:
                rel = str(p)
            try:
                content = Path(p).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                content = f"# ERROR READING FILE: {e}\n"
            result.append((rel, content))
        return result
    finally:
        for k, v in saved.items():
            if v is None and not hasattr(dm, k):
                continue
            setattr(dm, k, v)


def dump_git(ref, cwd):
    """Dump the tree at git `ref`, as if scanned from `cwd`'s relative
    position inside the repo. Uses `git archive` — never checks anything out.
    """
    compare = load_compare()
    cwd = Path(cwd).resolve()
    git_root = compare.get_git_root(cwd)
    sha = compare.git_rev_parse(git_root, ref)
    if sha is None:
        raise RuntimeError(f"cannot resolve git ref {ref!r}")
    try:
        rel = cwd.relative_to(git_root)
    except ValueError:
        raise RuntimeError(f"{cwd} is not under git root {git_root}")
    with tempfile.TemporaryDirectory(prefix="dumps_evolve_") as tmp:
        extract_dir = compare.extract_tree(sha, git_root, Path(tmp))
        wt_cwd = extract_dir / rel
        if not wt_cwd.is_dir():
            raise RuntimeError(f"directory {rel} does not exist at {ref}")
        return dump_dir(wt_cwd)


# --- Parsing / writing (delegated to dumps_diff) ---------------------------

def parse_file(path):
    _, files = load_diff().parse_dump(
        Path(path).read_text(encoding="utf-8"))
    return files


def write_file(path, title, files):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return load_diff().write_dump(path, title, "", files)


def count_lines(path):
    with open(path, encoding="utf-8") as fh:
        return sum(1 for _ in fh)


# --- Diff helpers ----------------------------------------------------------

def split(baseline, current):
    """Return (common, delta, deleted).

    common  = files in both, identical content           (from baseline)
    delta   = files in current that are new or modified  (from current)
    deleted = files in baseline absent from current
    """
    bmap = dict(baseline)
    cmap = dict(current)
    common  = sorted((p, c) for p, c in baseline
                     if p in cmap and cmap[p] == c)
    delta   = sorted((p, c) for p, c in current
                     if p not in bmap or bmap[p] != c)
    deleted = sorted(p for p in bmap if p not in cmap)
    return common, delta, deleted


# --- Adopting --latest -----------------------------------------------------

def adopt_latest(src_str):
    """Validate that `src_str` parses as a dump, then move it to latest.md."""
    src = Path(src_str)
    if not src.is_file():
        print(f"error: --latest file not found: {src}", file=sys.stderr)
        return False

    try:
        files = parse_file(src)
    except Exception as e:
        print(f"error: {src} does not parse as a dump: {e}", file=sys.stderr)
        return False
    if not files:
        print(f"error: {src} contains no FILE blocks", file=sys.stderr)
        return False

    if src.resolve() == LATEST_PATH.resolve():
        print(f"{LATEST_PATH.name} already in place ({len(files)} files)",
              file=sys.stderr)
        return True

    if LATEST_PATH.exists():
        print(f"warning: overwriting existing {LATEST_PATH.name}",
              file=sys.stderr)
    try:
        src.replace(LATEST_PATH)
    except OSError as e:
        print(f"error: cannot move {src} -> {LATEST_PATH}: {e}", file=sys.stderr)
        return False
    print(f"Adopted {src} -> {LATEST_PATH.name} ({len(files)} files)",
          file=sys.stderr)
    return True


# --- Modes -----------------------------------------------------------------

def cmd_run(args):
    cwd = Path.cwd()

    if args.latest:
        if not adopt_latest(args.latest):
            return 1

    if LATEST_PATH.exists():
        try:
            baseline = parse_file(LATEST_PATH)
        except Exception as e:
            print(f"error: cannot parse {LATEST_PATH}: {e}", file=sys.stderr)
            return 1
        baseline_desc = LATEST_PATH.name
    else:
        try:
            baseline = dump_git(DEFAULT_REF, cwd)
        except Exception as e:
            print(f"error: cannot dump git {DEFAULT_REF}: {e}", file=sys.stderr)
            return 1
        baseline_desc = f"git {DEFAULT_REF}"

    print(f"Reference: {baseline_desc}", file=sys.stderr)

    try:
        current = dump_dir(cwd)
    except Exception as e:
        print(f"error: cannot dump current state: {e}", file=sys.stderr)
        return 1

    common, delta, deleted = split(baseline, current)

    try:
        n_b = write_file(BASELINE_PATH,
                         f"Baseline — {len(common)} file(s) unchanged",
                         common)
        n_d = write_file(DELTA_PATH,
                         f"Delta — {len(delta)} file(s) changed or added",
                         delta)
    except OSError as e:
        print(f"error: cannot write outputs: {e}", file=sys.stderr)
        return 1

    print(file=sys.stderr)
    print(f"Wrote {BASELINE_PATH.name}: {n_b} file(s), "
          f"{count_lines(BASELINE_PATH)} lines", file=sys.stderr)
    print(f"Wrote {DELTA_PATH.name}:    {n_d} file(s), "
          f"{count_lines(DELTA_PATH)} lines", file=sys.stderr)
    if deleted:
        print(f"  (also: {len(deleted)} file(s) present in reference, "
              f"absent in cwd)", file=sys.stderr)
    if n_d == 0:
        print(f"  (no changes since {baseline_desc})", file=sys.stderr)
    return 0


def cmd_merge(delta_arg, latest_arg):
    delta_path = Path(delta_arg)
    latest_src = Path(latest_arg)
    for p in (delta_path, latest_src):
        if not p.is_file():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 1

    try:
        delta  = dict(parse_file(delta_path))
        latest = dict(parse_file(latest_src))
    except Exception as e:
        print(f"error: cannot parse inputs: {e}", file=sys.stderr)
        return 1

    merged = {**latest, **delta}          # delta overrides latest
    files = sorted(merged.items())

    tmp = LATEST_PATH.parent / (LATEST_PATH.name + ".tmp")
    try:
        n = write_file(tmp, f"Merged snapshot — {len(files)} file(s)", files)
        os.replace(tmp, LATEST_PATH)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        print(f"error: cannot write {LATEST_PATH}: {e}", file=sys.stderr)
        return 1

    print(f"Merged {delta_path.name} onto {latest_src.name} "
          f"-> {LATEST_PATH.name}", file=sys.stderr)
    print(f"  {n} file(s), {count_lines(LATEST_PATH)} lines", file=sys.stderr)
    return 0


# --- CLI -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="dumps_evolve.py",
        description="Delta dump: compare the working tree against latest.md "
                    "(or git HEAD), or merge two dumps into a new latest.md.",
    )
    ap.add_argument("--latest", metavar="FILE",
                    help="adopt FILE as latest.md (moved into place), then "
                         "diff cwd against it.")
    ap.add_argument("merge", nargs="*", metavar="FILE",
                    help="merge mode: DELTA LATEST -> new latest.md "
                         "(DELTA overrides LATEST on path collision).")
    args = ap.parse_args()

    if args.latest and args.merge:
        print("error: --latest cannot be combined with merge arguments",
              file=sys.stderr)
        return 2
    if len(args.merge) == 2:
        return cmd_merge(args.merge[0], args.merge[1])
    if len(args.merge) == 1:
        print("error: merge needs exactly two files: DELTA LATEST",
              file=sys.stderr)
        return 2
    if len(args.merge) > 2:
        print("error: merge takes at most two files", file=sys.stderr)
        return 2
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
