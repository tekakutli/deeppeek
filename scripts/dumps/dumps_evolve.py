#!/usr/bin/env python3
"""
dumps_evolve.py — two-file delta dump for iterative LLM review.

Modes:

  dumps_evolve.py
      Compare current working tree against latest.md (if present) else
      git HEAD. Writes:
        project_dump_<name>_baseline.md  — unchanged files + directory tree
        project_dump_<name>_delta.md     — git-style patch (a/ b/ prefixes,
                                           git-apply compatible) for added /
                                           modified / deleted files

  dumps_evolve.py --latest FILE
      Adopt FILE as latest.md (validated + moved into place), then as above.

  dumps_evolve.py DELTA LATEST
      Apply DELTA patch to LATEST full dump → new latest.md.

Typical workflow:

  1.  dumps_evolve.py                              # diff cwd vs HEAD
  2.  review baseline.md / delta.md
  3.  dumps_evolve.py project_dump_*_delta.md \
                     project_dump_*_baseline.md    # apply patch → latest.md
      (or: git apply project_dump_*_delta.md  in a checkout of the baseline)
  4.  edit code ...
  5.  dumps_evolve.py                              # diff cwd vs latest.md
  6.  repeat

Config (top of file):
  OVERRIDES   dump.py module variables to override (same keys as
              dumps_compare.py)
  DUMP_ARGS   extra CLI args for dump.py (default ["-r"])
"""

import argparse
import difflib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
LATEST_PATH  = Path("latest.md")
DEFAULT_REF  = "HEAD"

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

HEADER = "=" * 70
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


# --- dump.py config helpers ------------------------------------------------

def _dump_args_parsed():
    """Return (recursive_override, affix_override) parsed from DUMP_ARGS."""
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


def _scanned_dir(cwd):
    """Absolute scanned directory = cwd / DEFAULT_DIR (DEFAULT_DIR after
    OVERRIDES). For the common DEFAULT_DIR='.' this is just cwd."""
    dm = load_dump()
    default_dir = OVERRIDES.get("DEFAULT_DIR") \
                  or getattr(dm, "DEFAULT_DIR", ".") or "."
    return (Path(cwd) / default_dir).resolve()


def _own_output_names(cwd):
    """Filenames of our own outputs to skip during scans."""
    baseline_path, delta_path = output_paths_for(cwd)
    return {LATEST_PATH.name, baseline_path.name, delta_path.name}


def _is_own_output(name, cwd):
    return name in _own_output_names(cwd)


# --- Dumping ---------------------------------------------------------------

def dump_dir(scan_root):
    """Run dump.py's filters over `scan_root`; return [(rel_path, content)].

    `rel_path` is relative to the scanned directory (scan_root / DEFAULT_DIR),
    matching what `dump.py` emits in its FILE blocks.
    """
    dm = load_dump()
    saved = {}
    for k, v in OVERRIDES.items():
        saved[k] = getattr(dm, k, None)
        setattr(dm, k, v)
    try:
        scan_root = Path(scan_root)
        default_dir = getattr(dm, "DEFAULT_DIR", ".") or "."
        root = (scan_root / default_dir).resolve()

        recursive_ovr, affix_ovr = _dump_args_parsed()
        exts = [dm.normalize_ext(x) for x in getattr(dm, "DEFAULT_EXTENSIONS", [])]
        exts = [e for e in exts if e]
        affix = affix_ovr if affix_ovr is not None \
                else getattr(dm, "DEFAULT_AFFIX", None)
        if affix == "":
            affix = None
        exclude_dirs = [d for d in getattr(dm, "DEFAULT_EXCLUDE_DIRS", []) if d]
        include_dirs = {d for d in getattr(dm, "DEFAULT_INCLUDE_DIRS", []) if d}
        incl_sub = [s for s in getattr(dm, "DEFAULT_INCLUDE_FILE_SUBSTRINGS", []) if s]
        excl_sub = [s for s in getattr(dm, "DEFAULT_EXCLUDE_FILE_SUBSTRINGS", []) if s]
        recursive = recursive_ovr if recursive_ovr is not None \
                    else getattr(dm, "DEFAULT_RECURSIVE", False)

        paths = dm.find_files(root, exts, affix, exclude_dirs, include_dirs,
                              incl_sub, excl_sub, recursive)

        own = _own_output_names(scan_root)
        result = []
        for p in paths:
            if Path(p).name in own:
                continue
            try:
                rel = Path(p).resolve().relative_to(root)
            except ValueError:
                rel = Path(str(p))
            try:
                content = Path(p).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                content = f"# ERROR READING FILE: {e}\n"
            result.append((str(rel), content))
        return result
    finally:
        for k, v in saved.items():
            if v is None and not hasattr(dm, k):
                continue
            setattr(dm, k, v)


def dump_git(ref, cwd):
    """Dump the tree at git `ref`, as if scanned from `cwd`'s relative
    position inside the enclosing repo. Uses `git archive` — never checks
    anything out.
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


# --- Parsing / naming ------------------------------------------------------

def parse_dump_file(path):
    """Parse a full-dump file into [(path, content)]."""
    _, files = load_diff().parse_dump(Path(path).read_text(encoding="utf-8"))
    return files


def output_paths_for(cwd):
    """Return (baseline_path, delta_path):
       project_dump_<basename>_baseline.md
       project_dump_<basename>_delta.md
    """
    dm = load_dump()
    scanned = _scanned_dir(cwd)
    base = dm.output_path_with_dirname(Path("project_dump.md"), scanned)
    return (
        base.with_name(f"{base.stem}_baseline{base.suffix}"),
        base.with_name(f"{base.stem}_delta{base.suffix}"),
    )


# --- Patch generation ------------------------------------------------------

def make_patch_chunk(path, old_text, new_text):
    """Return a git-style patch for one file.

    old_text=None  → added file
    new_text=None  → deleted file
    both           → modified (or no-op if equal)
    """
    old_exists = old_text is not None
    new_exists = new_text is not None

    if old_exists and new_exists and old_text == new_text:
        return ""

    if old_exists and new_exists:
        header = f"diff --git a/{path} b/{path}\n"
        from_label, to_label = f"a/{path}", f"b/{path}"
        old_lines = old_text.splitlines(keepends=True) if old_text else []
        new_lines = new_text.splitlines(keepends=True) if new_text else []
    elif old_exists:
        header = f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n"
        from_label, to_label = f"a/{path}", "/dev/null"
        old_lines = old_text.splitlines(keepends=True) if old_text else []
        new_lines = []
    else:
        header = f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        from_label, to_label = "/dev/null", f"b/{path}"
        old_lines = []
        new_lines = new_text.splitlines(keepends=True) if new_text else []

    raw = list(difflib.unified_diff(
        old_lines, new_lines, fromfile=from_label, tofile=to_label,
        lineterm="\n",
    ))
    if not raw:
        return ""

    body = []
    for line in raw:
        if not line.endswith("\n"):
            body.append(line + "\n")
            body.append("\\ No newline at end of file\n")
        else:
            body.append(line)

    return header + "".join(body) + "\n"


def build_delta(baseline, current):
    """Return git-style patch text from baseline → current (added + modified
    + deleted files)."""
    bmap = dict(baseline)
    cmap = dict(current)
    chunks = []
    for path in sorted(set(bmap) | set(cmap)):
        chunk = make_patch_chunk(path, bmap.get(path), cmap.get(path))
        if chunk:
            chunks.append(chunk)
    return "".join(chunks)


def split_stats(baseline, current):
    bmap = dict(baseline)
    cmap = dict(current)
    common   = [p for p in cmap if p in bmap and bmap[p] == cmap[p]]
    modified = [p for p in cmap if p in bmap and bmap[p] != cmap[p]]
    added    = [p for p in cmap if p not in bmap]
    deleted  = [p for p in bmap if p not in cmap]
    return common, modified, added, deleted


# --- Writing outputs -------------------------------------------------------

def write_baseline(path, cwd, files):
    """Write baseline.md — SCANNED DIRECTORY + TREE + FILE blocks."""
    dm = load_dump()
    scanned = _scanned_dir(cwd)

    include_dirs = {d for d in getattr(dm, "DEFAULT_INCLUDE_DIRS", []) if d}
    tree_exclude = [d for d in getattr(dm, "DEFAULT_TREE_EXCLUDE_DIRS", []) if d]
    dumped_paths = [scanned / rel for rel, _ in files]

    tree_dict = dm.build_tree(scanned, include_dirs, tree_exclude, dumped_paths)
    tree_text = dm.render_tree(tree_dict, str(scanned))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(HEADER + "\n")
        fh.write(f"SCANNED DIRECTORY: {scanned}\n")
        fh.write(HEADER + "\n\n")

        fh.write(HEADER + "\n")
        fh.write("DIRECTORY TREE\n")
        fh.write(HEADER + "\n")
        fh.write("```\n")
        fh.write(tree_text + "\n")
        fh.write("```\n\n")

        total = len(files)
        for i, (rel, content) in enumerate(files, 1):
            fh.write(HEADER + "\n")
            fh.write(f"FILE {i}/{total}: {rel}\n")
            fh.write(HEADER + "\n")
            fh.write("```python\n")
            fh.write(content)
            if content and not content.endswith("\n"):
                fh.write("\n")
            fh.write("```\n\n")


def write_delta(path, cwd, baseline_desc, delta_text, stats):
    """Write delta.md — header comments + git-style patch."""
    common, modified, added, deleted = stats
    scanned = _scanned_dir(cwd)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("# Delta — generated by dumps_evolve.py\n")
        fh.write(f"# Baseline: {baseline_desc}\n")
        fh.write(f"# Current:  {scanned}\n")
        fh.write(f"# Stats: {len(modified)} modified, {len(added)} added, "
                 f"{len(deleted)} deleted, {len(common)} unchanged\n\n")
        if delta_text:
            fh.write(delta_text)
        else:
            fh.write("# (no changes)\n")


def write_latest(path, cwd, files):
    """Write latest.md — full dump with tree (same format as baseline)."""
    write_baseline(path, cwd, files)


# --- Patch application (for merge) -----------------------------------------

def extract_patch_body(text):
    """Strip leading '# comments' from a delta file; return patch text."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- "):
            return "".join(lines[i:])
    return text


def apply_patch(files, patch_text):
    """Apply git patch `patch_text` to `files` (as a temp dir), return the
    resulting [(path, content)] list."""
    with tempfile.TemporaryDirectory(prefix="dumps_evolve_apply_") as tmp_str:
        tmp = Path(tmp_str)
        for rel, content in files:
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8",
        ) as pf:
            pf.write(patch_text)
            patch_file = pf.name

        try:
            r = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", patch_file],
                cwd=tmp, capture_output=True, text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(f"git apply failed: {r.stderr.strip()}")
        finally:
            try:
                os.unlink(patch_file)
            except OSError:
                pass

        result = []
        for p in sorted(tmp.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(tmp)
            result.append((str(rel), p.read_text(encoding="utf-8")))
        return result


# --- Adopt -----------------------------------------------------------------

def adopt_latest(src_str):
    """Validate `src_str` as a dump and move it to latest.md."""
    src = Path(src_str)
    if not src.is_file():
        print(f"error: --latest file not found: {src}", file=sys.stderr)
        return False
    try:
        files = parse_dump_file(src)
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
        print(f"error: cannot move {src} -> {LATEST_PATH}: {e}",
              file=sys.stderr)
        return False
    print(f"Adopted {src} -> {LATEST_PATH.name} ({len(files)} files)",
          file=sys.stderr)
    return True


# --- Modes -----------------------------------------------------------------

def cmd_run(args):
    cwd = Path.cwd().resolve()

    if args.latest:
        if not adopt_latest(args.latest):
            return 1

    # Reference state
    if LATEST_PATH.exists():
        try:
            baseline = parse_dump_file(LATEST_PATH)
        except Exception as e:
            print(f"error: cannot parse {LATEST_PATH}: {e}", file=sys.stderr)
            return 1
        baseline_desc = LATEST_PATH.name
    else:
        try:
            baseline = dump_git(DEFAULT_REF, cwd)
        except Exception as e:
            print(f"error: cannot dump git {DEFAULT_REF}: {e}",
                  file=sys.stderr)
            return 1
        baseline_desc = f"git {DEFAULT_REF}"

    print(f"Baseline: {baseline_desc} ({len(baseline)} files)", file=sys.stderr)

    # Current working tree
    try:
        current = dump_dir(cwd)
    except Exception as e:
        print(f"error: cannot dump current state: {e}", file=sys.stderr)
        return 1
    print(f"Current:  {_scanned_dir(cwd)} ({len(current)} files)",
          file=sys.stderr)

    # Classify
    common, modified, added, deleted = split_stats(baseline, current)
    cmap = dict(baseline)
    baseline_files = [(p, cmap[p]) for p in common]

    # Delta patch
    delta_text = build_delta(baseline, current)

    # Output paths
    baseline_path, delta_path = output_paths_for(cwd)

    try:
        write_baseline(baseline_path, cwd, baseline_files)
        write_delta(delta_path, cwd, baseline_desc, delta_text,
                    (common, modified, added, deleted))
    except OSError as e:
        print(f"error: cannot write outputs: {e}", file=sys.stderr)
        return 1

    print(file=sys.stderr)
    print(f"Wrote {baseline_path.name}: {len(baseline_files)} file(s)",
          file=sys.stderr)
    print(f"Wrote {delta_path.name}:    {len(modified)} modified, "
          f"{len(added)} added, {len(deleted)} deleted", file=sys.stderr)
    if not modified and not added and not deleted:
        print(f"  (no changes vs {baseline_desc})", file=sys.stderr)
    return 0


def cmd_merge(delta_arg, latest_arg):
    delta_path = Path(delta_arg)
    latest_src = Path(latest_arg)
    for p in (delta_path, latest_src):
        if not p.is_file():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 1

    try:
        base_files = parse_dump_file(latest_src)
    except Exception as e:
        print(f"error: cannot parse {latest_src}: {e}", file=sys.stderr)
        return 1

    delta_text = delta_path.read_text(encoding="utf-8")
    patch_text = extract_patch_body(delta_text)
    if not patch_text.strip() or patch_text.lstrip().startswith("#"):
        print("error: delta contains no patch hunks", file=sys.stderr)
        return 1

    try:
        merged = apply_patch(base_files, patch_text)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    cwd = Path.cwd().resolve()
    try:
        write_latest(LATEST_PATH, cwd, merged)
    except OSError as e:
        print(f"error: cannot write {LATEST_PATH}: {e}", file=sys.stderr)
        return 1

    print(f"Merged {delta_path.name} onto {latest_src.name} "
          f"-> {LATEST_PATH.name}", file=sys.stderr)
    print(f"  {len(merged)} file(s)", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="dumps_evolve.py",
        description="Two-file delta dump: baseline (unchanged + tree) and "
                    "delta (git-style patch) against latest.md or git HEAD. "
                    "Or merge a patch-file delta onto a full dump to produce "
                    "a new latest.md.",
    )
    ap.add_argument("--latest", metavar="FILE",
                    help="adopt FILE as latest.md (moved into place), then "
                         "diff cwd against it.")
    ap.add_argument("merge", nargs="*", metavar="FILE",
                    help="merge mode: DELTA LATEST -> new latest.md "
                         "(DELTA is a patch file, LATEST a full dump).")
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
