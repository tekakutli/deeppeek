#!/usr/bin/env python3
"""Print a rich `git status`-style overview of a repository.

Combines:
  * `git status --porcelain=v2 --branch`         → branch / upstream / ahead-behind
  * `git diff`                                    → unstaged hunks
  * `git ls-files --others --exclude-standard`    → untracked files

Usage:
    status.py [-C PATH] [--color | --no-color]

The repository defaults to the current directory; pass `-C PATH` to point
at another one.  Colors are enabled when stdout is a TTY unless overridden.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys


class Ansi:
    RESET   = "\x1b[0m"
    BOLD    = "\x1b[1m"
    RED     = "\x1b[31m"
    GREEN   = "\x1b[32m"
    YELLOW  = "\x1b[33m"
    MAGENTA = "\x1b[35m"
    CYAN    = "\x1b[36m"


def paint(enabled, code, text):
    return f"{code}{text}{Ansi.RESET}" if enabled and code else text


def git(*args):
    # Always disable color for captured output.  We colorize ourselves,
    # and leftover ANSI codes in the raw diff would break the header and
    # hunk-marker parsing below (e.g. if the user has color.ui=always).
    return subprocess.run(
        ["git",
         "-c", "core.quotepath=false",
         "-c", "color.ui=never",
         "-c", "color.diff=never",
         "-c", "color.status=never",
         *args],
        capture_output=True, text=True, check=False,
    ).stdout


def read_branch():
    info = {"ahead": 0, "behind": 0}
    for line in git("status", "--porcelain=v2", "--branch").splitlines():
        for key, prefix in (("oid",      "# branch.oid "),
                            ("head",     "# branch.head "),
                            ("upstream", "# branch.upstream ")):
            if line.startswith(prefix):
                info[key] = line[len(prefix):]
        m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
        if m:
            info["ahead"], info["behind"] = int(m.group(1)), int(m.group(2))
    return info


def subject(*revs):
    return git("log", "-1", "--format=%s", *revs).strip()


# Two-character statuses that denote unmerged paths during a merge/rebase.
# These would otherwise map through their first letter and be reported as
# "deleted" / "new file" / etc., which is wrong.
_UNMERGED_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def pretty_status(code):
    """Map a `git diff --name-status` code to a human-readable word."""
    if code in _UNMERGED_CODES:
        return "unmerged"
    first = code[0] if code else "M"
    return {"M": "modified", "D": "deleted", "A": "new file",
            "R": "renamed",  "C": "copied",  "T": "typechange",
            "U": "unmerged"}.get(first, "modified")


def unstaged_files():
    files = []
    for line in git("diff", "--name-status").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append((parts[-1], pretty_status(parts[0])))
    return files


SKIP_DIFF_HEADER = (
    "index ", "--- ", "+++ ",
    "new file mode ", "deleted file mode ",
    "old mode ", "new mode ",
    "similarity index ", "dissimilarity index ",
    "rename from ", "rename to ",
    "copy from ", "copy to ",
)


def diff_bodies():
    """Return one diff body per section, in the same order as
    `git diff --name-status` (which is the order `unstaged_files()`
    uses).

    Combined diffs (`` diff --cc ``, produced for unmerged paths during
    a merge or rebase) are included so that every file listed by
    `--name-status` has a corresponding body slot; otherwise positional
    matching in `render()` would silently drop trailing files.
    """
    bodies, body, active = [], [], False
    for line in git("diff").splitlines():
        if line.startswith("diff --git ") or line.startswith("diff --cc "):
            if active:
                bodies.append("\n".join(body).rstrip("\n"))
            body, active = [], True
        elif active:
            if line.startswith(SKIP_DIFF_HEADER):
                continue
            body.append(line)
    if active:
        bodies.append("\n".join(body).rstrip("\n"))
    return bodies


def untracked_files():
    return [l for l in git("ls-files", "--others",
                           "--exclude-standard").splitlines() if l]


def colorize_body(body, enabled):
    if not enabled:
        return body
    out = []
    for line in body.splitlines():
        # `@@@` is the combined-diff hunk marker; `@@` the normal one.
        # `startswith("@@")` matches both.
        if line.startswith("@@"):
            out.append(paint(True, Ansi.CYAN,  line))
        elif line.startswith("+"):
            out.append(paint(True, Ansi.GREEN, line))
        elif line.startswith("-"):
            out.append(paint(True, Ansi.RED,   line))
        else:
            out.append(line)
    return "\n".join(out)


def render(color):
    info = read_branch()
    out = []

    head = info.get("head", "HEAD")
    if head == "(detached)":
        sha = info.get("oid", "")[:12]
        head_disp = paint(color, Ansi.BOLD, sha) if sha else head
        subj = subject(sha) if sha else ""
    elif head == "(initial)":
        # Empty repository: no commits yet, so nothing to describe.
        head_disp = paint(color, Ansi.MAGENTA, "(initial)")
        subj = ""
    else:
        head_disp = paint(color, Ansi.MAGENTA, head)
        subj = subject(head)
    out.append(f"{'Head:':<10}{head_disp} {subj}".rstrip())

    up = info.get("upstream")
    if up:
        up_disp = paint(color, Ansi.MAGENTA, up)
        if info["behind"]:
            out.append(f"{'Merge:':<10}{up_disp} "
                       f"{subject(f'HEAD..{up}')}".rstrip())
        if info["ahead"]:
            out.append(f"{'Push:':<10}{up_disp} "
                       f"{subject(f'{up}..HEAD')}".rstrip())

    out.append("")

    files = unstaged_files()
    bodies = diff_bodies()
    out.append(f"Unstaged changes ({len(files)})")

    # Position-based match is correct because both commands are driven
    # by the same diff and see the same file set in the same order.  Be
    # defensive anyway: if the counts ever diverge (e.g. some git config
    # or a future git behavior changes things), show every file rather
    # than silently truncating to the shorter side.
    for i, (path, word) in enumerate(files):
        body = bodies[i] if i < len(bodies) else ""
        pad = " " * max(0, 10 - len(word))
        out.append(f"{paint(color, Ansi.YELLOW, word)}{pad} {path}")
        if body:
            out.append(colorize_body(body, color))

    ut = untracked_files()
    if ut:
        out.append("")
        out.append(f"Untracked files ({len(ut)})")
        for p in ut:
            out.append(f"{'untracked':<10} {p}")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description="Print a status overview of a git repository.")
    ap.add_argument("-C", "--directory", metavar="PATH", default=None,
                    help="run as if started in PATH (default: cwd)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--color", dest="color", action="store_true",
                     help="always use ANSI colors")
    grp.add_argument("--no-color", dest="color", action="store_false",
                     help="never use ANSI colors")
    ap.set_defaults(color=None)
    args = ap.parse_args()

    if shutil.which("git") is None:
        sys.exit("git not found in PATH")

    color = args.color if args.color is not None else sys.stdout.isatty()

    if args.directory:
        try:
            os.chdir(args.directory)
        except OSError as e:
            sys.exit(f"cannot chdir to {args.directory!r}: {e}")

    if git("rev-parse", "--is-inside-work-tree").strip() != "true":
        sys.exit(f"not a git work tree: {os.getcwd()}")

    print(render(color))


if __name__ == "__main__":
    main()
