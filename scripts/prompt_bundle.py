#!/usr/bin/env python3
"""prompt_bundle.py — merge files via merge_md.py, wrap the result,
append a CURRENT PROMPT section, and (optionally) git-commit & push
it, then print its raw.githubusercontent.com URL.

Sibling of merge_md.py.  Takes no flags: every knob lives in CONFIG.

Pipeline
========

  1. merge_md.py (its sibling) is invoked on INPUT_FILES, writing to
     MERGED_PATH.
  2. MERGED_PATH is wrapped in a fence whose backtick run is strictly
     wider than any backtick run inside it.
  3. A `CURRENT PROMPT` section (bounded by `~` runs) is appended.
  4. The result is written to OUTPUT_PATH.  If OUTPUT_PATH names a
     directory (an existing dir, or a string ending in `/`), a
     timestamped `prompt_bundle_<stamp>.md` inside it is written.
  5. If GIT_PUSH is True: the containing repo is located, the output
     file is staged, committed, pushed to GIT_REMOTE, and the raw URL
     of the pushed file is printed to stdout.
  6. If GIT_PUSH is False: the bundle is simply created — no repo
     detection, no commit, no push, no URL.

Toggle
======

    GIT_PUSH = True   → commit + push + print raw URL
    GIT_PUSH = False  → just create the bundle

Usage
=====

Edit CONFIG, then:

    python3 prompt_bundle.py

No arguments.  No flags.
"""
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ====================================================================
# CONFIG — edit these, not flags
# ====================================================================

# Master toggle.  True: commit + push + print raw URL.
# False: just create the bundle (no git operations at all).
GIT_PUSH: bool = False

INPUT_FILES: list[str] = [
    "/home/tekakutli/files/Downloads/conversation(1).md",
    "/home/tekakutli/files/Downloads/project_dump_kritomatic_merged_cast.md",
    "/home/tekakutli/files/Downloads/project_dump_playgrounds(2).md",
    "/home/tekakutli/files/Downloads/project_dump_kritomatic_explicit.md"
]

CURRENT_PROMPT: str = """\
you had requested from me some files: kritomatic_daemon/utils/comfy_convert.py kritomatic_daemon/handlers/comfyui_workflow.py kritomatic_daemon/utils/comfyui_proxy_client.py kritomatic_daemon/handlers/comfyui_types.py kritomatic_daemon/utils/comfy_apply.py src/kritomatic_shared/comfyui_substitute.py src/kritomatic_shared/comfyui_tooling.py
is your architecture plan still fit or does it need more work? are we ready to implement it?
"""

MERGED_PATH: str = "/tmp/merged_context.md"
# OUTPUT_PATH: str = "/tmp/prompt_bundle.md"
OUTPUT_PATH: str = "/home/tekakutli/files/org/dotfiles/input_controller/krita_plugin/prompts/"
TITLE: str = "Prompt bundle"

# ---- git (only used when GIT_PUSH is True) ----
GIT_REMOTE: str = "origin"
GIT_COMMIT_MESSAGE: str = "Update prompt bundle ({timestamp})"

# ---- github raw url (only used when GIT_PUSH is True) ----
# GITHUB_USER is the repo owner; the repo name and branch are derived
# from GIT_REMOTE and HEAD respectively.
GITHUB_USER: str = "tekakutli"


# ====================================================================
# Implementation
# ====================================================================

HERE = Path(__file__).resolve().parent
MERGE_MD = HERE / "merge_md.py"

_MIN_FENCE_WIDTH = 3
_MIN_SEPARATOR_WIDTH = 70
_PROMPT_CHAR = "~"


# --------------------------------------------------------------------
# markdown helpers
# --------------------------------------------------------------------

def _max_run(text: str, ch: str) -> int:
    longest = current = 0
    for c in text:
        if c == ch:
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest


def _fence_for(content: str) -> str:
    return "`" * max(_MIN_FENCE_WIDTH, _max_run(content, "`") + 1)


def _prompt_header() -> str:
    bar = _PROMPT_CHAR * _MIN_SEPARATOR_WIDTH
    return f"{bar}\n# CURRENT PROMPT\n{bar}\n"


def _preamble(input_paths: list[Path]) -> str:
    lines = [
        "This document bundles reference material above and a task "
        "below.",
        "",
        f"Reference material — {len(input_paths)} file(s) merged by "
        "`merge_md.py`:",
        "",
    ]
    for i, p in enumerate(input_paths, 1):
        lines.append(f"  {i}. {p}")
    lines += [
        "",
        "Everything inside the outermost fence is that reference "
        "material.",
        "The `CURRENT PROMPT` section that follows is the task to "
        "perform on it.",
        "",
        "The wrapper fence is strictly wider than any backtick run in "
        "the merged content, so the merged document — which contains "
        "fences of its own — cannot end the wrapper early.",
        "",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------
# output-path resolution
# --------------------------------------------------------------------

def _resolve_output_path(raw: str) -> Path:
    p = Path(raw)
    if p.is_dir() or raw.endswith(("/", "\\")):
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return p / f"prompt_bundle_{stamp}.md"
    return p


# --------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=127,
            stdout="", stderr="git executable not found on PATH\n",
        )


def _find_git_root(start: Path) -> Path | None:
    r = _git("-C", str(start), "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return Path(out) if out else None


def _current_branch(repo_root: Path) -> str | None:
    r = _git("-C", str(repo_root), "branch", "--show-current")
    if r.returncode != 0:
        return None
    name = r.stdout.strip()
    return name or None


def _stage_and_commit(
    repo_root: Path, paths: list[Path], message: str,
) -> int:
    rel_paths: list[str] = []
    for p in paths:
        try:
            rel_paths.append(str(p.resolve().relative_to(repo_root)))
        except ValueError:
            print(f"warning: {p} is not inside {repo_root}; "
                  f"skipping from commit", file=sys.stderr)
    if not rel_paths:
        print("warning: no paths to stage", file=sys.stderr)
        return 0

    r = _git("-C", str(repo_root), "add", "--", *rel_paths)
    if r.returncode != 0:
        print(f"error: git add failed:\n{r.stderr.strip()}",
              file=sys.stderr)
        return 1

    r = _git("-C", str(repo_root), "status", "--porcelain",
             "--", *rel_paths)
    if r.returncode != 0:
        print(f"error: git status failed:\n{r.stderr.strip()}",
              file=sys.stderr)
        return 1
    if not r.stdout.strip():
        print("git: nothing to commit (output unchanged)",
              file=sys.stderr)
        return 0

    r = _git("-C", str(repo_root), "commit", "-m", message)
    if r.returncode != 0:
        print(
            f"error: git commit failed (file written and staged, "
            f"but not committed):\n{r.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    print(f"git: committed in {repo_root}: {message!r}",
          file=sys.stderr)
    return 0


def _push(repo_root: Path, remote: str, branch: str) -> int:
    r = _git("-C", str(repo_root), "push", remote, branch)
    if r.returncode == 0:
        print(f"git: pushed {branch} -> {remote}", file=sys.stderr)
        return 0
    r2 = _git("-C", str(repo_root), "push", "-u", remote, branch)
    if r2.returncode == 0:
        print(f"git: pushed {branch} -> {remote} (upstream set)",
              file=sys.stderr)
        return 0
    print(
        "error: git push failed:\n"
        f"  first attempt:  {r.stderr.strip()}\n"
        f"  retry (-u):     {r2.stderr.strip()}",
        file=sys.stderr,
    )
    return 1


# --------------------------------------------------------------------
# github raw-url helpers
# --------------------------------------------------------------------

def _repo_name_from_remote(remote_url: str) -> str | None:
    url = remote_url.strip()
    if not url:
        return None
    if url.endswith(".git"):
        url = url[:-4]
    url = url.rstrip("/")
    if "://" in url:
        url = url.split("://", 1)[1]
    m = re.match(r"[^:/]+[:/](.+)$", url)
    if not m:
        return None
    parts = m.group(1).split("/")
    if len(parts) < 2:
        return None
    return parts[-1]


def _raw_url(repo_root: Path, file_path: Path,
             branch: str) -> str | None:
    r = _git("-C", str(repo_root), "remote", "get-url", GIT_REMOTE)
    if r.returncode != 0:
        print(f"warning: cannot read remote {GIT_REMOTE!r}: "
              f"{r.stderr.strip()}", file=sys.stderr)
        return None
    repo = _repo_name_from_remote(r.stdout.strip())
    if repo is None:
        print(f"warning: cannot parse repo name from remote "
              f"{r.stdout.strip()!r}", file=sys.stderr)
        return None
    try:
        rel = file_path.resolve().relative_to(repo_root)
    except ValueError:
        print(f"warning: {file_path} is not inside {repo_root}",
              file=sys.stderr)
        return None
    return (
        f"https://raw.githubusercontent.com/"
        f"{GITHUB_USER}/{repo}/refs/heads/{branch}/"
        f"{rel.as_posix()}"
    )


# --------------------------------------------------------------------
# main
# --------------------------------------------------------------------

def main() -> int:
    if not MERGE_MD.is_file():
        print(f"error: merge_md.py not found next to this script "
              f"({MERGE_MD})", file=sys.stderr)
        return 1

    input_paths = [Path(p) for p in INPUT_FILES]
    if not input_paths:
        print("error: INPUT_FILES is empty", file=sys.stderr)
        return 1
    for p in input_paths:
        if not p.is_file():
            print(f"error: input file not found: {p}", file=sys.stderr)
            return 1

    merged = Path(MERGED_PATH)
    output = _resolve_output_path(OUTPUT_PATH)

    resolved_inputs = {p.resolve() for p in input_paths}
    for label, p in (("MERGED_PATH", merged),
                     ("OUTPUT_PATH", output)):
        if p.resolve() in resolved_inputs:
            print(f"error: {label} equals an input file: {p}",
                  file=sys.stderr)
            return 1
    if merged.resolve() == output.resolve():
        print("error: MERGED_PATH and OUTPUT_PATH are the same file",
              file=sys.stderr)
        return 1

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create {output.parent}: {e}",
              file=sys.stderr)
        return 1

    # ---- pre-check git (only if GIT_PUSH) ----
    # When GIT_PUSH is False, no repo interaction happens at all:
    # repo_root and branch stay None and every git step below is
    # skipped.
    repo_root: Path | None = None
    branch: str | None = None
    if GIT_PUSH:
        repo_root = _find_git_root(output.resolve().parent)
        if repo_root is None:
            print(
                f"error: GIT_PUSH is on but {output.parent} is not "
                f"inside a git repository",
                file=sys.stderr,
            )
            return 1
        branch = _current_branch(repo_root)
        if branch is None:
            print(
                f"error: cannot determine current branch in "
                f"{repo_root} (detached HEAD?)",
                file=sys.stderr,
            )
            return 1
        print(
            f"git: OUTPUT_PATH is inside repo {repo_root} "
            f"(branch {branch})",
            file=sys.stderr,
        )

    # ---- 1. produce the merged file via the sibling ----
    cmd = [
        sys.executable, str(MERGE_MD),
        "-o", str(merged),
        *[str(p) for p in input_paths],
    ]
    print("running: " + " ".join(cmd), file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"error: merge_md.py exited {result.returncode}",
              file=sys.stderr)
        return result.returncode

    # ---- 2. read merged content ----
    try:
        merged_text = merged.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read {merged}: {e}", file=sys.stderr)
        return 1

    # ---- 3. assemble the wrapper ----
    fence = _fence_for(merged_text)
    body_parts: list[str] = []
    if TITLE:
        body_parts.append(f"# {TITLE}\n\n")
    body_parts.append(_preamble(input_paths))
    body_parts.append("\n")
    body_parts.append(fence + "markdown\n")
    body_parts.append(merged_text)
    if merged_text and not merged_text.endswith("\n"):
        body_parts.append("\n")
    body_parts.append(fence + "\n\n")
    body_parts.append(_prompt_header())
    body_parts.append("\n")
    body_parts.append(CURRENT_PROMPT)
    if CURRENT_PROMPT and not CURRENT_PROMPT.endswith("\n"):
        body_parts.append("\n")
    body = "".join(body_parts)

    # ---- 4. write ----
    try:
        output.write_text(body, encoding="utf-8")
    except OSError as e:
        print(f"error: cannot write {output}: {e}", file=sys.stderr)
        return 1

    print(
        f"Wrote {output} "
        f"(context: {merged}, {len(input_paths)} file(s); "
        f"prompt: {len(CURRENT_PROMPT)} chars)",
        file=sys.stderr,
    )

    # ---- 5. commit + push + URL (only if GIT_PUSH) ----
    if GIT_PUSH and repo_root is not None and branch is not None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = GIT_COMMIT_MESSAGE.replace("{timestamp}", ts)

        if _stage_and_commit(repo_root, [output], msg) != 0:
            return 1
        if _push(repo_root, GIT_REMOTE, branch) != 0:
            return 1

        url = _raw_url(repo_root, output, branch)
        if url is not None:
            print(url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
