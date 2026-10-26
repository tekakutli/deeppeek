#!/usr/bin/env python3
"""
dumps_evolve.py — two-file delta dump for iterative LLM review.

Modes:

  dumps_evolve.py
      Compare current working tree against baseline.md (if present) else
      git HEAD. Writes:
        project_dump_<name>_baseline.md  — files UNCHANGED since baseline
                                           (+ directory tree)
        project_dump_<name>_delta.md     — git-style patch for added /
                                           modified / deleted files

  dumps_evolve.py --baseline FILE
      Adopt FILE as baseline.md (validated + moved into place), then as above.

  dumps_evolve.py --new-delta DELTA BASELINE
      Reconstruct reference = apply(DELTA, BASELINE) entirely in memory.
      Compare the working tree against that reconstruction, and write
      the standard output pair:
        project_dump_<name>_baseline.md  — files UNCHANGED vs reference
        project_dump_<name>_delta.md     — patch reference -> working tree
      baseline.md is NOT touched.  The reference stays as the
      (DELTA, BASELINE) pair the caller passed in — no materialized
      baseline.md is created or advanced.  Typical use: keep the pair
      from the previous run as-is, keep editing, and get a fresh delta.

      Auto-detects which argument is the dump vs the patch: if exactly
      one of the two files looks like a full dump (has `FILE i/N:`
      blocks), that file is treated as BASELINE regardless of the
      positional order.

  dumps_evolve.py DELTA BASELINE
      Apply DELTA patch to BASELINE full dump -> new baseline.md.

  dumps_evolve.py DELTA1 DELTA2
      Apply DELTA1 then DELTA2 onto the reference (baseline.md, or git
      HEAD if no baseline.md), and write the standard output pair.
      baseline.md is NOT modified.  Combined delta is immediately
      mergeable with `dumps_evolve.py COMBINED.md baseline.md`.

Note on the two uses of "baseline":

  · "baseline.md"          — the reference snapshot we diff against
                             (input to the run, or input to merge).
  · "_baseline.md" output  — the files that are IDENTICAL between that
                             reference and the current tree (output of
                             the run).
  They play different roles.  The presence or absence of the leading
  "project_dump_<name>_" prefix distinguishes them.

Typical workflow:

  1.  dumps_evolve.py                              # diff cwd vs HEAD
  2.  review project_dump_*_baseline.md / *_delta.md
  3.  dumps_evolve.py project_dump_*_delta.md \
                     project_dump_*_baseline.md    # apply patch -> baseline.md
  4.  edit code ...
  5.  dumps_evolve.py                              # diff cwd vs baseline.md
  6.  repeat

  Or keep the pair as reference (no baseline.md materialized):

  1.  dumps_evolve.py                              # -> pair (B0, D0)
  2.  edit code ...
  3.  dumps_evolve.py --new-delta D0 B0            # -> pair (B1, D1)
  4.  edit code ...
  5.  dumps_evolve.py --new-delta D1 B1            # -> pair (B2, D2)
  ...

Config (top of file):
  OVERRIDES   dump.py module variables to override (same keys as
              dumps_compare.py)
  DUMP_ARGS   extra CLI args for dump.py (default ["-r"])
"""

import argparse
import difflib
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
BASELINE_PATH = Path("baseline.md")
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
    """Filenames of our own outputs to skip during scans.

    Covers both the reference file (baseline.md) and the two derived
    outputs (project_dump_*_baseline.md, project_dump_*_delta.md), so
    a re-run can't accidentally ingest its own previous output.
    """
    baseline_path, delta_path = output_paths_for(cwd)
    return {BASELINE_PATH.name, baseline_path.name, delta_path.name}


# --- Dumping ---------------------------------------------------------------

def dump_dir(scan_root, extra_exclude_paths=None):
    """Run dump.py's filters over `scan_root`; return [(rel_path, content)].

    `rel_path` is relative to the scanned directory (scan_root / DEFAULT_DIR),
    matching what `dump.py` emits in its FILE blocks.

    `extra_exclude_paths` — optional list of absolute Paths to skip in
    addition to the well-known own-output names.  Used by --new-delta
    so the input files (which may live in the scanned directory and
    may have arbitrary names) don't appear in the resulting dump.
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
        exclude_full = {Path(p).resolve() for p in (extra_exclude_paths or [])}
        result = []
        for p in paths:
            p_resolved = Path(p).resolve()
            if p_resolved.name in own or p_resolved in exclude_full:
                continue
            try:
                rel = p_resolved.relative_to(root)
            except ValueError:
                rel = Path(str(p))
            try:
                content = p_resolved.read_text(encoding="utf-8", errors="replace")
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
    """Return (baseline_out_path, delta_out_path):

        project_dump_<name>_baseline.md
        project_dump_<name>_delta.md

    These are outputs — the *unchanged files* dump and the *patch* dump.
    They are distinct from BASELINE_PATH, the reference snapshot we
    diffed against.
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

    old_text=None  -> added file
    new_text=None  -> deleted file
    both           -> modified (or no-op if equal)
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
    """Return git-style patch text from baseline -> current (added +
    modified + deleted files)."""
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

def write_baseline_dump(path, cwd, files):
    """Write a dump file in dump.py format — SCANNED DIRECTORY + TREE +
    FILE blocks.  Used both for `project_dump_*_baseline.md` (the
    unchanged-files output) and for `baseline.md` itself (the reference
    snapshot after a merge)."""
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


def write_delta_dump(path, cwd, baseline_desc, delta_text, stats):
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


def write_snapshot(path, cwd, files):
    """Write the reference snapshot (baseline.md) — same format as
    project_dump_*_baseline.md."""
    write_baseline_dump(path, cwd, files)


# --- Patch application -----------------------------------------------------

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


def apply_patches_sequential(files, named_patches):
    """Apply each patch in order to `files` in a temp dir; return result.

    `named_patches` is a list of (label, patch_text) tuples.  Each
    patch is applied to the current state left by the previous one.
    On failure, the error names the patch that failed so the user can
    swap the argument order if needed.
    """
    with tempfile.TemporaryDirectory(prefix="dumps_evolve_seq_") as tmp_str:
        tmp = Path(tmp_str)
        for rel, content in files:
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        for label, patch_text in named_patches:
            if not patch_text.strip():
                continue
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
                    raise RuntimeError(
                        f"git apply ({label}) failed: {r.stderr.strip()}"
                    )
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


# --- Input classification --------------------------------------------------

_FILE_BLOCK_RE = re.compile(r"^FILE \d+/\d+: ", re.MULTILINE)
_DELTA_HEADER_RE = re.compile(r"^# Delta\b", re.MULTILINE)


def _is_dump_file(path):
    """True iff file looks like a full dump (has FILE i/N blocks).

    Dump detection takes precedence over delta detection: a dump may
    contain `diff --git` in a file it scanned (e.g. a test fixture
    holding a patch), but a delta cannot contain `FILE i/N:` blocks
    because those aren't valid patch syntax.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(_FILE_BLOCK_RE.search(text))


def _is_delta_file(path):
    """True iff file looks like a delta (git-style patch)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if _DELTA_HEADER_RE.search(text):
        return True
    if "diff --git " in text:
        return True
    return False


def _extract_delta_patches(path):
    """Return a list of patch-text strings from a delta file.

    A single delta file normally contains one patch body (possibly
    multiple `diff --git` hunks).  Empty deltas (the `# (no changes)`
    case) return an empty list — they're no-ops in a merge sequence.
    """
    text = path.read_text(encoding="utf-8")
    body = extract_patch_body(text)
    if not body.strip() or body.lstrip().startswith("#"):
        return []
    return [body]


# --- Adopt -----------------------------------------------------------------

def adopt_baseline(src_str):
    """Validate `src_str` as a dump and move it to baseline.md."""
    src = Path(src_str)
    if not src.is_file():
        print(f"error: --baseline file not found: {src}", file=sys.stderr)
        return False
    try:
        files = parse_dump_file(src)
    except Exception as e:
        print(f"error: {src} does not parse as a dump: {e}", file=sys.stderr)
        return False
    if not files:
        print(f"error: {src} contains no FILE blocks", file=sys.stderr)
        return False
    if src.resolve() == BASELINE_PATH.resolve():
        print(f"{BASELINE_PATH.name} already in place ({len(files)} files)",
              file=sys.stderr)
        return True
    if BASELINE_PATH.exists():
        print(f"warning: overwriting existing {BASELINE_PATH.name}",
              file=sys.stderr)
    try:
        src.replace(BASELINE_PATH)
    except OSError as e:
        print(f"error: cannot move {src} -> {BASELINE_PATH}: {e}",
              file=sys.stderr)
        return False
    print(f"Adopted {src} -> {BASELINE_PATH.name} ({len(files)} files)",
          file=sys.stderr)
    return True


# --- Loading the reference baseline ---------------------------------------

def _load_reference(cwd):
    """Return (baseline_files, baseline_desc, error_str_or_None).

    Reference is baseline.md if present, else git HEAD.  Both paths
    print the same shape of error on failure.
    """
    if BASELINE_PATH.exists():
        try:
            baseline = parse_dump_file(BASELINE_PATH)
        except Exception as e:
            return None, None, f"cannot parse {BASELINE_PATH}: {e}"
        return baseline, BASELINE_PATH.name, None
    try:
        baseline = dump_git(DEFAULT_REF, Path(cwd))
    except Exception as e:
        return None, None, f"cannot dump git {DEFAULT_REF}: {e}"
    return baseline, f"git {DEFAULT_REF}", None


# --- Modes -----------------------------------------------------------------

def cmd_run(args):
    cwd = Path.cwd().resolve()

    if args.baseline:
        if not adopt_baseline(args.baseline):
            return 1

    # Reference state
    if BASELINE_PATH.exists():
        try:
            baseline = parse_dump_file(BASELINE_PATH)
        except Exception as e:
            print(f"error: cannot parse {BASELINE_PATH}: {e}",
                  file=sys.stderr)
            return 1
        baseline_desc = BASELINE_PATH.name
    else:
        try:
            baseline = dump_git(DEFAULT_REF, cwd)
        except Exception as e:
            print(f"error: cannot dump git {DEFAULT_REF}: {e}",
                  file=sys.stderr)
            return 1
        baseline_desc = f"git {DEFAULT_REF}"

    print(f"Baseline: {baseline_desc} ({len(baseline)} files)",
          file=sys.stderr)

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
    baseline_out, delta_out = output_paths_for(cwd)

    try:
        write_baseline_dump(baseline_out, cwd, baseline_files)
        write_delta_dump(delta_out, cwd, baseline_desc, delta_text,
                         (common, modified, added, deleted))
    except OSError as e:
        print(f"error: cannot write outputs: {e}", file=sys.stderr)
        return 1

    print(file=sys.stderr)
    print(f"Wrote {baseline_out.name}: {len(baseline_files)} file(s)",
          file=sys.stderr)
    print(f"Wrote {delta_out.name}:    {len(modified)} modified, "
          f"{len(added)} added, {len(deleted)} deleted", file=sys.stderr)
    if not modified and not added and not deleted:
        print(f"  (no changes vs {baseline_desc})", file=sys.stderr)
    return 0


# ---- new-delta: reconstruct (baseline+delta) in memory, then diff -------

def cmd_new_delta(args):
    """Diff the working tree against a reference reconstructed in memory
    from (baseline, delta).  Writes the standard output pair; does NOT
    touch baseline.md.
    """
    cwd = Path.cwd().resolve()

    raw_a = Path(args.new_delta[0])
    raw_b = Path(args.new_delta[1])
    for p in (raw_a, raw_b):
        if not p.is_file():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 1

    # Auto-detect which is the dump.  A dump has FILE i/N blocks; a
    # delta has git-style patch hunks.  Order is `--new-delta DELTA
    # BASELINE`, but we accept either order because swapping two file
    # paths is a trivial mistake with an opaque failure otherwise.
    a_dump = _is_dump_file(raw_a)
    b_dump = _is_dump_file(raw_b)
    if a_dump and b_dump:
        print("error: both files are full dumps — need exactly one "
              "delta and one baseline.", file=sys.stderr)
        return 2
    if a_dump and not b_dump:
        baseline_path, delta_path = raw_a, raw_b
    elif b_dump and not a_dump:
        baseline_path, delta_path = raw_b, raw_a
    else:
        # Neither is a dump.  Refuse — an "--new-delta" with two deltas
        # is ambiguous (which is the baseline?) and would produce an
        # inscrutable parse error later.
        print("error: neither file looks like a full dump "
              "(no `FILE i/N:` blocks).", file=sys.stderr)
        print("  --new-delta expects: DELTA (patch) + BASELINE (full dump).",
              file=sys.stderr)
        return 2

    # Load baseline
    try:
        base_files = parse_dump_file(baseline_path)
    except Exception as e:
        print(f"error: cannot parse {baseline_path}: {e}", file=sys.stderr)
        return 1
    if not base_files:
        print(f"error: {baseline_path} contains no FILE blocks",
              file=sys.stderr)
        return 1

    # Reconstruct reference = baseline + delta, in memory
    delta_text = delta_path.read_text(encoding="utf-8")
    patch_text = extract_patch_body(delta_text)
    if not patch_text.strip() or patch_text.lstrip().startswith("#"):
        print(f"note: {delta_path.name} is an empty delta (no-op)",
              file=sys.stderr)
        reference = base_files
    else:
        try:
            reference = apply_patch(base_files, patch_text)
        except RuntimeError as e:
            print(f"error: cannot apply {delta_path.name} to "
                  f"{baseline_path.name}: {e}", file=sys.stderr)
            return 1

    ref_desc = f"{baseline_path.name} + {delta_path.name}"
    print(f"Reference: {ref_desc} ({len(reference)} files)",
          file=sys.stderr)

    # Current working tree — exclude the two input files so they can't
    # leak into the delta (they may be arbitrary filenames).
    try:
        current = dump_dir(
            cwd, extra_exclude_paths=[baseline_path, delta_path],
        )
    except Exception as e:
        print(f"error: cannot dump current state: {e}", file=sys.stderr)
        return 1
    print(f"Current:  {_scanned_dir(cwd)} ({len(current)} files)",
          file=sys.stderr)

    # Split and write outputs
    common, modified, added, deleted = split_stats(reference, current)
    rmap = dict(reference)
    baseline_files = [(p, rmap[p]) for p in common]
    delta_out_text = build_delta(reference, current)

    baseline_out, delta_out = output_paths_for(cwd)
    try:
        write_baseline_dump(baseline_out, cwd, baseline_files)
        write_delta_dump(delta_out, cwd, ref_desc, delta_out_text,
                         (common, modified, added, deleted))
    except OSError as e:
        print(f"error: cannot write outputs: {e}", file=sys.stderr)
        return 1

    print(file=sys.stderr)
    print(f"Wrote {baseline_out.name}: {len(baseline_files)} file(s)",
          file=sys.stderr)
    print(f"Wrote {delta_out.name}:    {len(modified)} modified, "
          f"{len(added)} added, {len(deleted)} deleted", file=sys.stderr)
    if not modified and not added and not deleted:
        print(f"  (no changes vs {ref_desc})", file=sys.stderr)
    return 0


# ---- merge: single delta onto dump (advances baseline.md) ----------------

def _merge_delta_onto_dump(delta_path, dump_path):
    """Apply one delta to a full dump, write the result to baseline.md."""
    try:
        base_files = parse_dump_file(dump_path)
    except Exception as e:
        print(f"error: cannot parse {dump_path}: {e}", file=sys.stderr)
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
        write_snapshot(BASELINE_PATH, cwd, merged)
    except OSError as e:
        print(f"error: cannot write {BASELINE_PATH}: {e}", file=sys.stderr)
        return 1

    print(f"Merged {delta_path.name} onto {dump_path.name} "
          f"-> {BASELINE_PATH.name}", file=sys.stderr)
    print(f"  {len(merged)} file(s)", file=sys.stderr)
    return 0


# ---- merge: two deltas (produces a combined delta, no baseline change) ----

def _merge_two_deltas(d1_path, d2_path):
    """Apply D1 then D2 onto the reference.  Write the standard output
    pair (unchanged-files dump + combined delta).  baseline.md is NOT
    modified — the reference stays as it was, so the combined delta is
    immediately mergeable with `dumps_evolve.py COMBINED.md baseline.md`.
    """
    cwd = Path.cwd().resolve()

    baseline, baseline_desc, err = _load_reference(cwd)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"Reference: {baseline_desc} ({len(baseline)} files)",
          file=sys.stderr)

    d1_patches = _extract_delta_patches(d1_path)
    d2_patches = _extract_delta_patches(d2_path)
    if not d1_patches and not d2_patches:
        print("error: both deltas contain no patch hunks", file=sys.stderr)
        return 1
    if not d1_patches:
        print(f"note: {d1_path.name} is an empty delta (no-op)",
              file=sys.stderr)
    if not d2_patches:
        print(f"note: {d2_path.name} is an empty delta (no-op)",
              file=sys.stderr)

    named_patches = (
        [(d1_path.name, p) for p in d1_patches]
        + [(d2_path.name, p) for p in d2_patches]
    )
    try:
        result = apply_patches_sequential(baseline, named_patches)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        print("  deltas are applied left-to-right.  If the conflict is "
              "order-related, swap the arguments:",
              file=sys.stderr)
        print(f"    dumps_evolve.py {d2_path.name} {d1_path.name}",
              file=sys.stderr)
        return 1

    combined_delta = build_delta(baseline, result)
    common, modified, added, deleted = split_stats(baseline, result)
    cmap = dict(baseline)
    unchanged_files = [(p, cmap[p]) for p in common]

    baseline_out, delta_out = output_paths_for(cwd)
    combined_desc = (
        f"{baseline_desc} + {d1_path.name} + {d2_path.name}"
    )
    try:
        write_baseline_dump(baseline_out, cwd, unchanged_files)
        write_delta_dump(
            delta_out, cwd, combined_desc, combined_delta,
            (common, modified, added, deleted),
        )
    except OSError as e:
        print(f"error: cannot write outputs: {e}", file=sys.stderr)
        return 1

    print(file=sys.stderr)
    print(f"Combined {d1_path.name} + {d2_path.name} over "
          f"{baseline_desc}", file=sys.stderr)
    print(f"  {len(modified)} modified, {len(added)} added, "
          f"{len(deleted)} deleted, {len(common)} unchanged",
          file=sys.stderr)
    print(f"  wrote {baseline_out.name} ({len(unchanged_files)} file(s))",
          file=sys.stderr)
    print(f"  wrote {delta_out.name}", file=sys.stderr)
    print(f"  ({BASELINE_PATH.name} left untouched)", file=sys.stderr)
    return 0


# ---- merge: dispatch on the pair -----------------------------------------

def cmd_merge(a_arg, b_arg):
    a, b = Path(a_arg), Path(b_arg)
    for p in (a, b):
        if not p.is_file():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 1

    a_dump = _is_dump_file(a)
    b_dump = _is_dump_file(b)

    if a_dump and b_dump:
        print("error: both files are full dumps — nothing to merge.",
              file=sys.stderr)
        print("  expected: (DELTA DUMP) or (DELTA DELTA).",
              file=sys.stderr)
        return 2

    if a_dump and not b_dump:
        if not _is_delta_file(b):
            print(f"error: {b.name} is neither a delta nor a dump",
                  file=sys.stderr)
            return 2
        return _merge_delta_onto_dump(b, a)

    if b_dump and not a_dump:
        if not _is_delta_file(a):
            print(f"error: {a.name} is neither a delta nor a dump",
                  file=sys.stderr)
            return 2
        return _merge_delta_onto_dump(a, b)

    if _is_delta_file(a) and _is_delta_file(b):
        return _merge_two_deltas(a, b)

    print("error: cannot classify inputs.", file=sys.stderr)
    print("  expected: (DELTA DUMP) or (DELTA DELTA).", file=sys.stderr)
    print("  a delta has `diff --git` hunks; a dump has `FILE i/N:` "
          "blocks.", file=sys.stderr)
    return 2


def main():
    ap = argparse.ArgumentParser(
        prog="dumps_evolve.py",
        description="Two-file delta dump: baseline (unchanged + tree) and "
                    "delta (git-style patch) against baseline.md or git HEAD. "
                    "Also merges: DELTA + BASELINE advances baseline.md; "
                    "DELTA + DELTA produces a combined delta.",
    )
    ap.add_argument(
        "--baseline", metavar="FILE",
        help="adopt FILE as baseline.md (moved into place), then "
             "diff cwd against it.",
    )
    ap.add_argument(
        "--new-delta", nargs=2, metavar=("DELTA", "BASELINE"),
        help="reconstruct reference = BASELINE + DELTA in memory, then "
             "diff the working tree against that reconstruction.  "
             "Writes the standard output pair; does NOT modify "
             "baseline.md.  Auto-detects which argument is which.",
    )
    ap.add_argument(
        "merge", nargs="*", metavar="FILE",
        help="merge mode.  Two forms:\n"
             "  DELTA BASELINE  -> apply DELTA to BASELINE, write "
             "baseline.md\n"
             "  DELTA1 DELTA2   -> apply DELTA1 then DELTA2 to the "
             "reference (baseline.md or git HEAD); write the standard "
             "output pair, leave baseline.md untouched.",
    )
    args = ap.parse_args()

    if args.baseline and args.new_delta:
        print("error: --baseline and --new-delta are mutually exclusive "
              "(they both set the reference; pick one).",
              file=sys.stderr)
        return 2
    if args.new_delta and args.merge:
        print("error: --new-delta cannot be combined with merge arguments.",
              file=sys.stderr)
        return 2
    if args.baseline and args.merge:
        print("error: --baseline cannot be combined with merge arguments",
              file=sys.stderr)
        return 2

    if args.new_delta:
        return cmd_new_delta(args)
    if len(args.merge) == 2:
        return cmd_merge(args.merge[0], args.merge[1])
    if len(args.merge) == 1:
        print("error: merge needs exactly two files", file=sys.stderr)
        return 2
    if len(args.merge) > 2:
        print("error: merge takes at most two files", file=sys.stderr)
        return 2
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
