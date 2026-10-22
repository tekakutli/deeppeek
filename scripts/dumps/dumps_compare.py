#!/usr/bin/env python3
"""
Dump two git revisions of a project via dump.py, for diffing.

For each commit, this:
    extract that commit's tree into a temporary directory (via `git archive`)
    load dump.py from the extracted tree  (so that commit's version is used)
    apply OVERRIDES to its module-level config variables
    run dump.py there                     (writes e.g. project_dump_myproject.md)
    read the output into memory; the temp dir is then discarded

`dump.py` is always run with its cwd set to the extracted equivalent of the
directory you invoked this script from, so `DEFAULT_DIR = "."` in dump.py
scans *that* directory — matching the standalone `cd dir && python dump.py`
workflow. If you invoke this script from outside the git repository, the
scan root falls back to the directory containing this script.

Finally, both dumps are written to the directory you invoked this script from:

    project_dump_myproject_previous.md
    project_dump_myproject_current.md

Because the working tree is never checked out, this is safe to run with
uncommitted changes to tracked files — including edits to dump.py or to this
script itself. The dumps reflect the committed state of each revision.

Refs (PREVIOUS_COMMIT, CURRENT_COMMIT) are resolved to absolute SHAs at start,
so `HEAD~1` / `HEAD` keep their meaning throughout the run.

Extra flags for dump.py (e.g. -r, -t, --affix AFFIX) can be set permanently
via DUMP_ARGS below, or passed ad-hoc after `--`:

    dumps_compare.py -- -r --affix cable

Run it as:

    python3 dumps_compare.py

(Invoke it from wherever you want the final .md files to appear.)
"""

import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURATION — edit these
# ---------------------------------------------------------------------------
PREVIOUS_COMMIT = "HEAD~1"     # e.g. a SHA, tag, branch, HEAD~3, HEAD@{1}, ...
CURRENT_COMMIT  = "HEAD"

# If you rewrote your current commit (amend / rebase / reset+recommit) and
# you want the PRE-rewrite snapshot as `previous`, use git's own reflog syntax:
#     PREVIOUS_COMMIT = "HEAD@{1}"
# (Plain "HEAD~1" is the parent commit and is unaffected by amending.)

# Extra CLI args passed to dump.py on both runs.
# e.g. ["-r"] for recursion, ["-r", "--affix", "cable"] for recursion + affix,
# ["-t"] for tree-only. DUMP_ARGS is applied first; then anything given after
# `--` on the command line is appended. Both are appended to dump.py's argv.
DUMP_ARGS = ["-r"]

# Overrides applied to dump.py's module-level variables for BOTH runs.
# Use exactly the names from the top of dump.py. Anything dump.py reads at
# run time (DEFAULT_DIR, DEFAULT_OUT, DEFAULT_EXTENSIONS, DEFAULT_AFFIX,
# DEFAULT_RECURSIVE, DEFAULT_TREE_ONLY, DEFAULT_EXCLUDE_DIRS,
# DEFAULT_TREE_EXCLUDE_DIRS, DEFAULT_INCLUDE_DIRS,
# DEFAULT_INCLUDE_FILE_SUBSTRINGS, DEFAULT_EXCLUDE_FILE_SUBSTRINGS) can be
# overridden here.
#
# NB: a non-empty DEFAULT_INCLUDE_DIRS is a whitelist of subdir basenames —
# it prunes everything not listed, *even* with `-r`. Leave it out (or set to
# []) unless you actually want to restrict descent.
OVERRIDES = {
    # "DEFAULT_INCLUDE_DIRS": ["thisDir"],
    # "DEFAULT_EXTENSIONS": [".py", ".sh"],
    # "DEFAULT_RECURSIVE": True,
}

# Suffixes inserted before the extension of each dump's filename.
PREVIOUS_SUFFIX = "_previous"
CURRENT_SUFFIX  = "_current"

# Name of the script to drive, relative to this file's directory. Both this
# script and dump.py are expected to live in the same directory.
DUMP_SCRIPT = "dump.py"

# Notes / caveats for the archive-based extraction:
#  * DEFAULT_DIR in dump.py is resolved relative to the extracted equivalent
#    of the invocation cwd. If it's "." (the default), you get exactly what
#    standalone `cd <invocation dir> && python dump.py` would produce.
#  * `git archive` respects export-ignore / export-subst in .gitattributes,
#    and does not include submodule contents. If dump.py itself were
#    export-ignore'd, we'd error out below.
# ---------------------------------------------------------------------------


# --- git helpers -----------------------------------------------------------

def get_git_root(path: Path) -> Path:
    """Return the top-level directory of the enclosing git repo."""
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"not a git repo at {path}")
    return Path(r.stdout.strip()).resolve()


def git_rev_parse(cwd: Path, ref: str):
    """Return the full SHA of `ref`, or None if it doesn't resolve."""
    r = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=cwd, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def resolve_commits(cwd: Path, prev_spec: str, curr_spec: str):
    """Pin PREVIOUS_COMMIT and CURRENT_COMMIT to absolute SHAs *now*."""
    prev_sha = git_rev_parse(cwd, prev_spec)
    if prev_sha is None:
        raise RuntimeError(f"cannot resolve PREVIOUS_COMMIT = {prev_spec!r}")
    curr_sha = git_rev_parse(cwd, curr_spec)
    if curr_sha is None:
        raise RuntimeError(f"cannot resolve CURRENT_COMMIT = {curr_spec!r}")
    return prev_sha, curr_sha


def shortstat_between(cwd: Path, prev_sha: str, curr_sha: str):
    """Human-readable summary of what changes between the two commits."""
    r = subprocess.run(
        ["git", "diff", "--shortstat", prev_sha, curr_sha],
        cwd=cwd, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


# --- dump.py driving -------------------------------------------------------

def load_dump_module(dump_path: Path):
    """Load dump.py as a *fresh* module object (no caching), so each commit's
    version of the script is used."""
    spec = importlib.util.spec_from_file_location("dump_instance", str(dump_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {dump_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # does not run main()
    return module


def extract_tree(commit_sha: str, git_root: Path, dest_parent: Path) -> Path:
    """Extract the tree at `commit_sha` into `dest_parent` (silently)."""
    archive_path = dest_parent / "tree.tar"
    r = subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive_path}",
         commit_sha],
        cwd=git_root,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git archive {commit_sha[:12]} failed (exit {r.returncode})"
        )

    extract_dir = dest_parent / "_extract"
    extract_dir.mkdir()
    with tarfile.open(archive_path) as tf:
        try:
            tf.extractall(extract_dir, filter="data")  # Python 3.12+
        except TypeError:
            tf.extractall(extract_dir)                 # older Python
    return extract_dir


def _diagnose_empty_scan(scan_root: Path, user_cwd: Path, module, commit_sha: str):
    """Print a helpful diagnostic when dump.py reports 'no files found'."""
    print(f"\n  [diagnostic] dump.py found no matching files at commit "
          f"{commit_sha[:12]}", file=sys.stderr)
    print(f"    scan root (extracted): {scan_root}", file=sys.stderr)
    print(f"    scan root (original):  {user_cwd}", file=sys.stderr)

    if not scan_root.is_dir():
        print(f"    (directory does not exist at this commit)", file=sys.stderr)
        return

    try:
        entries = sorted(scan_root.iterdir(),
                         key=lambda p: (p.is_file(), p.name))
    except OSError as e:
        print(f"    (could not list: {e})", file=sys.stderr)
        return

    if not entries:
        print(f"    (empty directory)", file=sys.stderr)
    else:
        print(f"    top-level entries ({len(entries)}):", file=sys.stderr)
        for e in entries[:20]:
            kind = "dir " if e.is_dir() else "file"
            print(f"      [{kind}] {e.name}", file=sys.stderr)
        if len(entries) > 20:
            print(f"      ... and {len(entries) - 20} more", file=sys.stderr)

    hints = []
    recursive = getattr(module, "DEFAULT_RECURSIVE", False)
    tree_only = getattr(module, "DEFAULT_TREE_ONLY", False)
    dump_args = list(getattr(module, "_dumps_compare_active_args", []))
    is_recursive = (recursive or tree_only
                    or "-r" in dump_args or "--recursive" in dump_args)
    if any(e.is_dir() for e in entries) and not is_recursive:
        hints.append('pass `-r` (recursion) — e.g. `DUMP_ARGS = ["-r"]` '
                     'or `dumps_compare.py -- -r`')

    include_dirs = getattr(module, "DEFAULT_INCLUDE_DIRS", []) or []
    if include_dirs:
        hints.append(f"DEFAULT_INCLUDE_DIRS = {list(include_dirs)} is a "
                     f"subdir whitelist that prunes everything else; "
                     f"clear it to walk all subdirs")

    ext_list = getattr(module, "DEFAULT_EXTENSIONS", []) or []
    if ext_list:
        hints.append(f"DEFAULT_EXTENSIONS = {list(ext_list)}; widen if your "
                     f"files have other extensions")

    if hints:
        print(f"    hint:", file=sys.stderr)
        for h in hints:
            print(f"      - {h}", file=sys.stderr)


def dump_at_commit(commit_sha, suffix, label, git_root, rel_repo_root,
                   user_cwd, dump_script_name, overrides, dump_args):
    """Extract `commit_sha`, run dump.py from the extracted tree with
    `overrides` and CLI flags `dump_args`, capture the output into memory,
    and return (final_name, content, file_count, line_count).

    dump.py's cwd is set to the extracted equivalent of `user_cwd` (the
    directory dumps_compare.py was invoked from), so dump.py's own
    DEFAULT_DIR = "." scans the same directory the user was standing in.
    The caller's working tree is never touched.

    `label` is only used in error messages; no progress is printed on
    success. The caller is responsible for the final summary.
    """
    with tempfile.TemporaryDirectory(prefix="dumps_compare_") as tmp_str:
        tmp = Path(tmp_str)
        extract_dir = extract_tree(commit_sha, git_root, tmp)

        # dump.py lives next to this script in the original repo; find its
        # extracted equivalent.
        wt_dump = extract_dir / rel_repo_root / dump_script_name
        if not wt_dump.is_file():
            raise RuntimeError(
                f"{dump_script_name} not present in the tree at "
                f"{commit_sha[:12]} (missing or excluded via .gitattributes)"
            )

        # The scan root is the invocation-cwd equivalent in the extracted
        # tree — matching `dump.py`'s own DEFAULT_DIR = "." semantics.
        try:
            rel_user_cwd = user_cwd.relative_to(git_root)
        except ValueError:
            # Invocation dir lives outside the repo — fall back to the
            # directory containing dumps_compare.py.
            rel_user_cwd = rel_repo_root
        wt_user_cwd = extract_dir / rel_user_cwd
        if not wt_user_cwd.is_dir():
            raise RuntimeError(
                f"directory {str(rel_user_cwd)!r} does not exist at "
                f"commit {commit_sha[:12]}"
            )

        old_cwd = Path.cwd()
        os.chdir(wt_user_cwd)
        try:
            module = load_dump_module(wt_dump)
            for key, value in overrides.items():
                if not hasattr(module, key):
                    print(f"  warning: dump.py has no variable {key!r} at "
                          f"{commit_sha[:12]}; override ignored", file=sys.stderr)
                setattr(module, key, value)
            module._dumps_compare_active_args = list(dump_args)

            saved_argv = sys.argv
            sys.argv = [str(wt_dump), *dump_args]
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout_buf), \
                     contextlib.redirect_stderr(stderr_buf):
                    rc = module.main()
            finally:
                sys.argv = saved_argv

            scanned_dir = Path(os.path.abspath(str(Path(module.DEFAULT_DIR))))
            actual = module.output_path_with_dirname(
                Path(module.DEFAULT_OUT), scanned_dir,
            )

            if rc != 0:
                # Re-emit dump.py's messages (indented) so failures remain
                # debuggable, then add the diagnostic + raise.
                captured = stdout_buf.getvalue() + stderr_buf.getvalue()
                for line in captured.splitlines():
                    print(f"  {line}", file=sys.stderr)
                if rc == 1:
                    _diagnose_empty_scan(wt_user_cwd, user_cwd, module,
                                         commit_sha)
                try:
                    actual.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"dump.py exited with {rc} at {commit_sha[:12]}"
                )

            if not actual.is_file():
                raise RuntimeError(f"expected dump output {actual} not found")

            content = actual.read_text(encoding="utf-8")

            # Parse useful counts out of dump.py's (captured) output; fall
            # back to computing them from the content if parsing fails.
            m = re.search(r"Total lines in output:\s*(\d+)",
                          stdout_buf.getvalue())
            line_count = int(m.group(1)) if m else len(content.splitlines())
            m = re.search(r"Wrote\s+(\d+)\s+file\(s\)",
                          stderr_buf.getvalue())
            file_count = int(m.group(1)) if m else None
        finally:
            os.chdir(old_cwd)

        final_name = f"{actual.stem}{suffix}{actual.suffix}"
        return final_name, content, file_count, line_count


# --- main ------------------------------------------------------------------

def _split_passthrough_args(argv):
    """Split `argv` (excluding argv[0]) into (our_args, dump_py_args) using
    `--` as the separator. Anything before `--` we don't currently accept,
    so if any is present we error out rather than silently ignore it."""
    if "--" in argv:
        idx = argv.index("--")
        ours, passthrough = argv[:idx], argv[idx + 1:]
    else:
        ours, passthrough = argv, []
    return ours, passthrough


def _is_under(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def main() -> int:
    user_cwd = Path.cwd()  # captured before anything can chdir away
    repo_root = Path(__file__).resolve().parent

    our_args, cli_dump_args = _split_passthrough_args(sys.argv[1:])
    if our_args:
        print(f"error: unexpected arguments {our_args!r}. "
              f"Pass dump.py flags after `--` (e.g. "
              f"`{Path(sys.argv[0]).name} -- -r`).", file=sys.stderr)
        return 2
    dump_args = list(DUMP_ARGS) + cli_dump_args

    try:
        git_root = get_git_root(repo_root)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        rel_repo_root = repo_root.relative_to(git_root)
    except ValueError:
        print(f"error: {repo_root} is not under git root {git_root}",
              file=sys.stderr)
        return 1

    try:
        prev_sha, curr_sha = resolve_commits(git_root, PREVIOUS_COMMIT,
                                             CURRENT_COMMIT)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Previous commit: {prev_sha}  ({PREVIOUS_COMMIT!r})", file=sys.stderr)
    print(f"Current commit:  {curr_sha}  ({CURRENT_COMMIT!r})", file=sys.stderr)

    # Only mention the scan root when it isn't just cwd — i.e. the fallback
    # case (invocation dir outside the git repo), which the user can't infer.
    if not _is_under(user_cwd, git_root):
        print(f"Scan root:       {repo_root}  "
              f"(invocation dir is outside the git repo; "
              f"falling back to script dir)", file=sys.stderr)

    if prev_sha == curr_sha:
        print("warning: PREVIOUS and CURRENT resolve to the same commit; "
              "both dumps will be identical. Check your refs.", file=sys.stderr)
    else:
        stat = shortstat_between(git_root, prev_sha, curr_sha)
        if stat:
            print(f"diff {prev_sha[:8]}..{curr_sha[:8]}: {stat}", file=sys.stderr)
        else:
            print("diff: (no content differences between these commits)",
                  file=sys.stderr)
    if dump_args:
        print(f"dump.py args:    {dump_args}", file=sys.stderr)
    print(file=sys.stderr)  # blank separator before "Done."

    dump_script_name = Path(DUMP_SCRIPT).name

    try:
        prev_name, prev_content, prev_files, prev_lines = dump_at_commit(
            prev_sha, PREVIOUS_SUFFIX, "previous", git_root, rel_repo_root,
            user_cwd, dump_script_name, OVERRIDES, dump_args,
        )
        curr_name, curr_content, curr_files, curr_lines = dump_at_commit(
            curr_sha, CURRENT_SUFFIX, "current", git_root, rel_repo_root,
            user_cwd, dump_script_name, OVERRIDES, dump_args,
        )
    except Exception as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1

    # Final save — both files land in the directory this script was invoked
    # from. No transient dump.py output touches the original working tree.
    prev_path = user_cwd / prev_name
    curr_path = user_cwd / curr_name
    try:
        prev_path.write_text(prev_content, encoding="utf-8")
        curr_path.write_text(curr_content, encoding="utf-8")
    except OSError as e:
        print(f"error: failed to write dumps: {e}", file=sys.stderr)
        return 1

    print("\nDone.", file=sys.stderr)
    for label, path, files, lines in (
        ("previous", prev_path, prev_files, prev_lines),
        ("current",  curr_path, curr_files, curr_lines),
    ):
        files_str = str(files) if files is not None else "?"
        print(f"  {label:>8}: {path.name}  ({files_str} files, {lines} lines)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
