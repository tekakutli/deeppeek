"""`deeppeek prompt review` — apply, then review the aftermath.

Pipeline (same shape as `evolve.py`, but with a deterministic
prelude):

    assemble   verify we're in a git repo; warn if no deeppeek/ dir
    reset      git reset --hard HEAD && git clean -fd
    apply      deeppeek recover <conversation> --apply-here
    status     run scripts/status.py IN CWD → <ws>/diff.txt
    pipe       deeppeek pipe <ws>/diff.txt
    pipe       deeppeek pipe <cwd>/deeppeek/*   (top-level files)
    type       deeppeek type <prompt — cwd + conversation templated>

The reset+apply prelude is what makes the review *reproducible*:
the diff is always against a clean HEAD, never against whatever a
previous run happened to leave behind.  There is no opt-out — if
you want to review an existing aftermath without regenerating it,
run `deeppeek pipe` and `deeppeek type` by hand.

Hard requirement, checked BEFORE any destructive operation:
`git rev-parse --is-inside-work-tree` must succeed.  If it does not,
we refuse — `git reset --hard` on a non-repo would be silent and
useless, and we'd rather bail with a clear message.

`--text STR` REPLACES THE COMPOSER CONTENT
==========================================
When `--text STR` is given, `STR` is typed into the DeepSeek
composer — exactly that string, nothing else, no concatenation,
no wrapping.  The templated default prompt is not used.  The rest
of the pipeline is unchanged: reset runs, apply runs, status is
captured, meta files are collected, and the pipe stage attaches
them as file chips.  The composer receives the file chips AND the
override text; the file chips are not part of the text field, so
the "only text in the text field" claim still holds.

Use cases for `--text`:

  · "just send a one-liner about this aftermath" — pass a short
    string and let the attachments speak for themselves.
  · "use a completely different prompt template" — craft it
    externally, pass it in.
  · scripting: feed a prompt from a file via
    `--text "$(cat prompt.md)"`.

The override string is used verbatim.  It is NOT `str.format`-ed
against the pipeline's kwargs — if you want the target path or
the conversation filename in there, `pwd` in the shell you are
running from (or use `--dry-run` to see them) and paste.  This
keeps the semantics dead simple: what you pass is what gets typed.
To suppress the attachments too (composer text only, no file
chips), add `--no-pipe`.

NON-ZERO EXIT FROM RECOVER IS NOT FATAL
=======================================
`deeppeek recover` uses non-zero exit codes to signal partial
application:

    1 — fail-closed: ≥1 file fell below --min-trust
    2 — skipped/missing files, basename collisions, [DEL] misses
    3 — validation error before recovery started
    4 — import-graph problems (missing modules / missing exports)

(`write.pass9_write` accumulates them via `max()`, so exit 4
dominates exit 1 when both happen.)  Those partial outcomes are
*precisely* what this command exists to review — aborting on them
would defeat the purpose.  The real gate is whether `deeppeek/`
exists after the apply (see `_stage_require_meta`).  The exit code
is recorded and surfaced in the typed prompt so the model knows
what it's looking at, but it does not abort the pipeline.

Informational warning, before the reset: if `<cwd>/deeppeek/` does
not exist, print a note that the user is about to nuke a tree that
does not (yet) look like a deeppeek target.  This is *not* a
blocker — the reset+apply will recreate `deeppeek/` anyway — but it
is the cheapest possible safety rail against running the script in
the wrong directory.

After the apply, `deeppeek/` is a hard requirement.  If it does not
exist post-apply, the apply silently failed; bail rather than
review nothing.

`deeppeek/low_trust/` and `deeppeek/cache/` are NOT piped — those
are per-file post-patch bodies and the LLM prompt cache, not the
top-level report.  `manifest.json` names every low-trust file, so
the model can ask for one specifically if it needs the body.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STATUS_PY_SCRIPT = _PROJECT_ROOT / "scripts" / "status.py"
DEFAULT_CONVERSATION = Path("/tmp/conversation.md")


# `deeppeek recover` exit codes, surfaced to the model as a
# human-readable gloss on the number.
_RECOVER_EXIT_GLOSS = {
    0: "clean run — everything the tool attempted landed",
    1: "fail-closed — ≥1 file fell below --min-trust, target preserved",
    2: "partial — some files skipped, missing, or [DEL] targets not found",
    3: "validation error before recovery started",
    4: "import-graph problems (missing modules or exports)",
}


# DEFAULT_PROMPT_TEMPLATE = """\
# I just ran `cd {target} && deeppeek recover {conversation} --apply-here`,
# on the clean original pre-applied state, and this is the aftermath.
# The recover run exited with code {recover_rc} ({recover_rc_gloss}).
# Exit codes are accumulated via `max()`, so a higher number can
# mask a lower one — read the reports for the full picture rather
# than assuming the run was all-or-nothing.

# Attached:

#   1. `diff.txt` — a `git status`-style view of {target} after the
#      apply (unstaged hunks + untracked files).  This is exactly what
#      deeppeek changed on disk, taken at {target}.
#   2. The top-level files under {meta_name}/ — the metadata artifacts
#      this run wrote alongside the target tree:
#        · `manifest.json` — per-file state, trust, patches applied or
#          failed, sanity rejections, escalations
#        · `warnings.txt` — everything deeppeek flagged during the run
#        · `deleted.txt` — files marked [DEL] and whether they were
#          actually removed from the target tree
#        · `MISSING_FILES.txt` — files referenced in the transcript but
#          never recovered

# Please review what deeppeek actually did at {target}:

#   · Did the patch extraction succeed?  Were the right files, blocks,
#     and edits identified?
#   · Did the apply produce correct, minimal changes?  Anything
#     over-deleted, silently dropped, or applied to the wrong file?
#   · What should deeppeek improve — in extraction, classification,
#     pairing, patching, or the sanity gate — so the next
#     `--apply-to` run needs less manual repair?

# Be concrete: name the pass, the file, the pattern.

# If you need the post-patch body of a specific file that sanity-gate
# rejected or that fell below --min-trust, ask me to attach it from
# `{meta_name}/low_trust/`.
# """
DEFAULT_PROMPT_TEMPLATE = """\
results
"""


# ---------------------------------------------------------------------------
# Subprocess helpers — deliberately the same shape as evolve.py; share
# them when a third prompt needs them.
# ---------------------------------------------------------------------------

def _deeppeek_cmd() -> list[str]:
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _run_cmd(cmd: list[str], *,
             cwd: Optional[Path] = None,
             timeout: Optional[float] = None,
             allow_failure: bool = False) -> tuple[int, str, str]:
    prefix = f"    $ (cd {cwd} && " if cwd else "    $ "
    suffix = ")" if cwd else ""
    print(f"{prefix}{' '.join(cmd)}{suffix}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        msg = f"`{cmd[0]}` not on PATH"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    except subprocess.TimeoutExpired:
        msg = f"timed out after {timeout}s"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    if proc.returncode != 0:
        tag = "(allowed)" if allow_failure else ""
        print(f"    exit {proc.returncode} {tag}".rstrip(),
              file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Pre-reset checks
# ---------------------------------------------------------------------------

def _stage_verify_git(cwd: Path) -> None:
    """Hard requirement: the reset step needs a git work tree.  Refuse
    before touching anything if there isn't one."""
    rc, stdout, _ = _run_cmd(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd, allow_failure=True,
    )
    if rc != 0 or stdout.strip() != "true":
        print(
            f"error: {cwd} is not a git work tree.",
            file=sys.stderr,
        )
        print(
            "  this command performs `git reset --hard HEAD` and "
            "`git clean -fd`",
            file=sys.stderr,
        )
        print(
            "  before reviewing — that requires a git checkout.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"[git] work tree: {cwd}", file=sys.stderr)


def _stage_warn_if_no_meta(cwd: Path) -> None:
    """Informational.  If `deeppeek/` is absent, the user is about to
    `git reset --hard` a tree that doesn't look like a deeppeek
    target.  Not a blocker — the apply step will create it — but
    worth flagging so a wrong-directory invocation is caught early."""
    if (cwd / "deeppeek").is_dir():
        return
    print(
        f"warning: {cwd}/deeppeek/ does not exist yet.",
        file=sys.stderr,
    )
    print(
        "  this script will run `git reset --hard HEAD` + "
        "`git clean -fd`,",
        file=sys.stderr,
    )
    print(
        "  then `deeppeek recover <conversation> --apply-here` — "
        "which creates deeppeek/.",
        file=sys.stderr,
    )
    print(
        "  if you didn't mean to nuke this tree, Ctrl+C now.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Reset + apply
# ---------------------------------------------------------------------------

def _stage_reset(cwd: Path) -> None:
    print(f"[reset] git reset --hard HEAD in {cwd}", file=sys.stderr)
    rc, _, _ = _run_cmd(
        ["git", "reset", "--hard", "HEAD"],
        cwd=cwd, allow_failure=False,
    )
    if rc != 0:
        raise SystemExit(f"error: `git reset --hard HEAD` exited {rc}")
    print(f"[reset] git clean -fd in {cwd}", file=sys.stderr)
    rc, _, _ = _run_cmd(
        ["git", "clean", "-fd"],
        cwd=cwd, allow_failure=False,
    )
    if rc != 0:
        raise SystemExit(f"error: `git clean -fd` exited {rc}")


def _stage_apply(cwd: Path, args: argparse.Namespace) -> int:
    """Run `recover --apply-here` in `cwd` and return its exit code.

    Non-zero exit is NOT an abort condition: `deeppeek recover` uses
    non-zero exit codes to signal partial application (1 = fail-
    closed, 2 = skipped/missing/collisions, 4 = import-graph
    problems).  Those are the outcomes this command exists to
    review.  A hard failure shows up as a missing `deeppeek/`
    directory, which `_stage_require_meta` catches immediately
    after this returns.

    The returned code is threaded into the typed prompt so the
    model knows the recovery outcome without having to reconstruct
    it from the reports.
    """
    print(f"[apply] recover {args.conversation} --apply-here in {cwd}",
          file=sys.stderr)
    prefix = _deeppeek_cmd()
    cmd = prefix + [
        "recover", str(args.conversation), "--apply-here",
    ]
    rc, _, _ = _run_cmd(
        cmd, cwd=cwd,
        timeout=args.recover_timeout, allow_failure=True,
    )
    if rc != 0:
        print(
            f"  recover exited {rc} — continuing; the reports under "
            f"deeppeek/ record the partial outcome",
            file=sys.stderr,
        )
    return rc


# ---------------------------------------------------------------------------
# Post-apply checks and captures
# ---------------------------------------------------------------------------

def _stage_require_meta(cwd: Path) -> Path:
    """Hard requirement, after the apply: deeppeek/ must exist.  If it
    doesn't, the apply silently failed; bail rather than review
    nothing."""
    meta = cwd / "deeppeek"
    if not meta.is_dir():
        raise SystemExit(
            f"error: {meta} was not created by the apply step.\n"
            f"  recover --apply-here appears to have run without "
            f"writing reports.\n"
            f"  check the recover output above."
        )
    return meta


def _resolve_workspace(args: argparse.Namespace) -> Path:
    if args.workspace is None:
        return Path(tempfile.mkdtemp(prefix="deeppeek_review_"))
    ws = args.workspace.resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _stage_status(cwd: Path, ws: Path,
                  args: argparse.Namespace) -> Path:
    """Run scripts/status.py in `cwd` (explicitly — not inherited) and
    write a self-describing diff.txt capturing both stdout and any
    stderr."""
    script = args.status_py or _STATUS_PY_SCRIPT
    if not script.exists():
        raise SystemExit(
            f"error: status script not found at {script}\n"
            f"  pass --status-py to point at a different script"
        )

    out = ws / "diff.txt"
    cmd = [sys.executable, str(script)]
    print(f"[status] running {script.name} in {cwd}", file=sys.stderr)
    rc, stdout, stderr = _run_cmd(
        cmd, cwd=cwd, timeout=args.status_timeout, allow_failure=True,
    )

    parts = [
        "# git status capture produced by scripts/status.py",
        f"# cwd:       {cwd}",
        f"# exit code: {rc}",
        "",
        stdout.rstrip(),
    ]
    if stderr.strip():
        parts += ["", "# stderr:", stderr.rstrip()]
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"  wrote {out} ({out.stat().st_size:,} bytes)",
          file=sys.stderr)
    return out


def _stage_pipe(paths: list[Path], args: argparse.Namespace) -> int:
    """Attach `paths` via `deeppeek pipe`.  Directories in the list are
    skipped with a note — same contract as `pipe` itself, but filtered
    here so the log stays short."""
    files = [p for p in paths if p.is_file()]
    skipped = [p for p in paths if not p.is_file()]
    for p in skipped:
        print(f"  skipped (not a regular file): {p}", file=sys.stderr)
    if not files:
        print("  (nothing to pipe — skipping)", file=sys.stderr)
        return 0

    prefix = _deeppeek_cmd()
    print(f"[pipe] attaching {len(files)} file(s):", file=sys.stderr)
    for f in files:
        print(f"  {f}  ({f.stat().st_size:,} bytes)", file=sys.stderr)
    rc, _, _ = _run_cmd(
        prefix + ["pipe"] + [str(f) for f in files],
        timeout=args.pipe_timeout,
        allow_failure=False,
    )
    return rc


def _stage_type(text: str, args: argparse.Namespace) -> int:
    fd, path = tempfile.mkstemp(
        prefix="deeppeek_review_", suffix=".md",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[type] {len(text):,} chars via `deeppeek type`",
              file=sys.stderr)
        prefix = _deeppeek_cmd()
        rc, _, _ = _run_cmd(
            prefix + ["type", path],
            timeout=args.type_timeout,
            allow_failure=False,
        )
        return rc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Prompt assembly — cwd, conversation, meta dir, recover_rc templated in
# ---------------------------------------------------------------------------

def _recover_rc_gloss(rc: int) -> str:
    if rc in _RECOVER_EXIT_GLOSS:
        return _RECOVER_EXIT_GLOSS[rc]
    # Unknown code — could be a user-supplied handler, a wrapper,
    # or a signal.  Say so without guessing.
    return "unrecognised exit code; read the reports directly"


def _build_prompt(cwd: Path, meta_dir: Path,
                  conversation: Path, recover_rc: int) -> str:
    return DEFAULT_PROMPT_TEMPLATE.format(
        target=cwd.resolve(),
        meta_name=meta_dir.name or "deeppeek",
        conversation=str(conversation),
        recover_rc=recover_rc,
        recover_rc_gloss=_recover_rc_gloss(recover_rc),
    )


def _resolve_composer_text(args: argparse.Namespace,
                           cwd: Path,
                           meta_dir: Path,
                           recover_rc: int) -> tuple[str, str]:
    """Return (text, source_tag) for the composer.

    When `--text STR` is given, STR is returned verbatim — no
    formatting, no wrapping, no concatenation with the default
    template.  Otherwise the templated default is built from the
    pipeline's state.  `source_tag` is a short label used only in
    the log line so the user can see which path was taken.
    """
    if args.text is not None:
        return args.text, "--text override"
    return (
        _build_prompt(cwd, meta_dir, args.conversation, recover_rc),
        "default template",
    )



def _stage_emit_parallel(prompt, files, args):
    """Open one composer session, fire attach + type back-to-back,
    collect acks.  Replaces the sequential pipe + type subprocess
    calls, roughly halving the wall-time cost of the emit stage.

    `files` may be empty/None to skip the pipe event; `prompt` may be
    None to skip the type event.  If both are absent, this is a no-op
    (returns 0).  Empty-string `prompt` (but not None) is an error,
    matching `commands/type.py`'s rejection of empty composer text.
    """
    import base64
    import mimetypes
    from ..commands._composer import broadcast_many, composer_session

    if prompt is not None and not prompt.strip():
        print("error: composer text is empty/whitespace-only",
              file=sys.stderr)
        return 1

    event_specs = []

    if files:
        payload_files = []
        for f in files:
            if not f.is_file():
                print(f"  skipped (not a regular file): {f}",
                      file=sys.stderr)
                continue
            data = f.read_bytes()
            mime = (mimetypes.guess_type(f.name)[0]
                    or "application/octet-stream")
            payload_files.append({
                "name": f.name,
                "mime": mime,
                "base64": base64.b64encode(data).decode("ascii"),
            })
            print(f"  {f.name}  ({len(data):,} bytes)",
                  file=sys.stderr)
        if payload_files:
            event_specs.append({
                "tag": "pipe",
                "event": "attach",
                "payload": {
                    "files": payload_files,
                    "opts": {"delay_ms": 0},
                },
                "ack_name": "attach",
            })

    if prompt:
        event_specs.append({
            "tag": "type",
            "event": "type",
            "payload": {"text": prompt},
            "ack_name": "type",
        })

    if not event_specs:
        return 0

    timeout = max(
        getattr(args, "pipe_timeout", 180.0),
        getattr(args, "type_timeout", 300.0),
    )
    with composer_session(port=getattr(args, "port", 8765),
                         timeout=timeout) as server:
        if server is None:
            return 1
        print(f"[parallel] firing {len(event_specs)} event(s):",
              file=sys.stderr)
        for s in event_specs:
            print(f"  {s['event']} (tag={s['tag']})",
                  file=sys.stderr)
        results = broadcast_many(server, event_specs, timeout=timeout)
        rc = 0
        for s in event_specs:
            r = results.get(s["tag"],
                            {"exit_code": 1, "result": {}})
            if r["exit_code"] == 0:
                print(f"  OK - {s['tag']}", file=sys.stderr)
            else:
                reason = (r["result"].get("reason")
                          or r["result"].get("error")
                          or "(no reason)")
                print(f"  FAIL - {s['tag']}: {reason}",
                      file=sys.stderr)
                if rc == 0:
                    rc = r["exit_code"]
        return rc


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    print(f"[review] cwd: {cwd}", file=sys.stderr)

    # 0. Cheap upfront validation of --text so we don't nuke the
    #    tree and *then* discover the override is empty.  `deeppeek
    #    type` would reject empty text anyway; failing here gives a
    #    clearer error and does not reset anything.
    if args.text is not None and not args.text.strip():
        print("error: --text is empty (or whitespace-only).  "
              "Omit --text to use the default prompt.",
              file=sys.stderr)
        return 2

    # 1. Hard: must be a git work tree (reset depends on it).
    _stage_verify_git(cwd)

    # 2. Informational: warn if this tree has never been applied to.
    _stage_warn_if_no_meta(cwd)

    # 3. Reset + apply (the deterministic prelude).  Apply's exit
    #    code is captured, NOT treated as fatal — partial outcomes
    #    are the whole reason this command exists.
    _stage_reset(cwd)
    recover_rc = _stage_apply(cwd, args)

    # 4. Hard: deeppeek/ must exist after the apply.  This is the
    #    real gate — if recovery ran, it wrote its reports, and
    #    those are what we're about to review.
    meta_dir = _stage_require_meta(cwd)
    print(f"  metadata: {meta_dir}", file=sys.stderr)

    # 5. Workspace, status capture, meta file list.
    ws = _resolve_workspace(args)
    print(f"  workspace: {ws}", file=sys.stderr)
    print(f"  (kept after run for inspection; rm -rf {ws} when done)",
          file=sys.stderr)

    status_path = _stage_status(cwd, ws, args)

    meta_files = sorted(p for p in meta_dir.iterdir() if p.is_file())
    print(f"  metadata files: {len(meta_files)}", file=sys.stderr)
    for f in meta_files:
        print(f"    {f.name}", file=sys.stderr)

    composer_text, source_tag = _resolve_composer_text(
        args, cwd, meta_dir, recover_rc,
    )
    print(f"[build] composer text: {len(composer_text):,} chars "
          f"({source_tag})", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] would pipe:", file=sys.stderr)
        print(f"  {status_path}", file=sys.stderr)
        for f in meta_files:
            print(f"  {f}", file=sys.stderr)
        print(f"\n[dry-run] recover exited {recover_rc}", file=sys.stderr)
        print(f"[dry-run] composer text ({source_tag}) below; not typed.",
              file=sys.stderr)
        print(composer_text)
        return 0

    if args.no_pipe:
        files = []
    else:
        files = [status_path, *meta_files]
    return _stage_emit_parallel(composer_text, files, args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "review",
        help="Reset, re-apply the conversation, and pipe the aftermath "
             "to DeepSeek for review",
    )

    p.add_argument(
        "--conversation", type=Path, default=DEFAULT_CONVERSATION,
        help=f"conversation markdown to re-apply before reviewing "
             f"(default: {DEFAULT_CONVERSATION})",
    )
    p.add_argument(
        "--text", default=None, metavar="STR",
        help="Override the default templated prompt.  STR is typed "
             "into the DeepSeek composer verbatim — exactly that "
             "string, nothing else, no concatenation with the "
             "default template.  The rest of the pipeline (reset, "
             "apply, status capture, pipe) still runs and the "
             "captured diff + meta files are still attached as "
             "file chips.  Use --no-pipe to suppress the "
             "attachments as well.  The string is NOT "
             "str.format-ed; if you want the target path in there, "
             "paste it (run --dry-run first to see what's "
             "available).",
    )
    p.add_argument(
        "--workspace", type=Path, default=None,
        help="directory for the captured diff.txt.  Default: a fresh "
             "temp dir in /tmp.  Point this at a stable location to "
             "keep the capture around for inspection.",
    )
    p.add_argument(
        "--status-py", type=Path, default=None,
        help=f"override the status.py path "
             f"(default: {_STATUS_PY_SCRIPT}).  Must accept no "
             f"arguments and print a diff-style view to stdout.",
    )
    p.add_argument(
        "--no-pipe", action="store_true",
        help="skip the pipe step; only type the composer text "
             "(useful when the attachments are already in the "
             "composer, or when you want only --text in the "
             "composer with no file chips at all)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="run the reset + apply, capture the status, print the "
             "file list and the composer text, then stop — no "
             "pipe, no type",
    )
    p.add_argument(
        "--recover-timeout", type=float, default=1800.0,
        help="seconds for `deeppeek recover --apply-here` (default: "
             "1800)",
    )
    p.add_argument(
        "--status-timeout", type=float, default=60.0,
        help="seconds for scripts/status.py (default: 60)",
    )
    p.add_argument(
        "--pipe-timeout", type=float, default=180.0,
        help="seconds for each `deeppeek pipe` (default: 180)",
    )
    p.add_argument(
        "--type-timeout", type=float, default=300.0,
        help="seconds for `deeppeek type` (default: 300)",
    )

    p.set_defaults(func=run)
